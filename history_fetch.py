"""Collect historical candles for Solana pools via GeckoTerminal / CoinGecko onchain OHLCV.

    python history_fetch.py discover --out data/pools_seen.csv --minutes 600 [--preset pro --key K]
    python history_fetch.py fetch --pools data/pools_seen.csv --out data/candles/ --days 14 [--preset pro --key K]

From CoinGecko's docs (docs.coingecko.com/reference/pool-ohlcv-contract-address):
  GET {base}/onchain/networks/{network}/pools/{pool}/ohlcv/{timeframe}?aggregate=1&before_timestamp=&limit<=1000
      &currency=usd&include_empty_intervals=false
  ohlcv_list rows are [timestamp, open, high, low, close, volume]; sort order is NOT stated (the example is
  newest-first), so we sort. Empty intervals (no swaps) are omitted by default. Basic plan history: 6 months.
  To page back set before_timestamp = oldest returned timestamp - 1.
NOT confirmed: keyless ("public") base URL/path and rate limits. They are presets you can override.

SURVIVORSHIP: collect pools at (or soon after) creation with `discover`, then fetch ALL of them later,
including pools that died. A pool list built afterwards from what still trades silently drops every rug.
pools.csv keeps a status per pool (ok / empty / error / partial) so the loss is visible.
"""
import argparse
import csv
import math
import os
import sys
import time
from datetime import datetime, timezone

from discovery import PRESETS as DISCOVERY_PRESETS
from net import http_json

OHLCV = {
    "pro": ("https://pro-api.coingecko.com/api/v3", "/onchain/networks/{network}/pools/{pool}/ohlcv/{tf}", "x-cg-pro-api-key"),
    "public": ("https://api.geckoterminal.com/api/v2", "/networks/{network}/pools/{pool}/ohlcv/{tf}", None),
}


def parse_ohlcv(body):
    """-> ascending list of (ts, o, h, l, c, v); malformed or impossible rows are dropped."""
    try:
        rows = body["data"]["attributes"]["ohlcv_list"]
    except (KeyError, TypeError):
        return []
    out = {}
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, (list, tuple)) or len(r) < 6:
            continue
        try:
            ts, o, h, l, c, v = (float(x) for x in r[:6])
        except (TypeError, ValueError):
            continue
        if not all(math.isfinite(x) for x in (ts, o, h, l, c, v)) or min(o, h, l, c) <= 0 or v < 0:
            continue
        out[ts] = (ts, o, h, l, c, v)
    return [out[k] for k in sorted(out)]


class GeckoOHLCV:
    def __init__(self, preset="pro", api_key=None, http=None, base=None, path=None, min_interval_s=2.1,
                 clock=time.time, sleep=time.sleep, backoff_s=20.0, max_retries=4):
        self.backoff_s, self.max_retries, self.throttled = backoff_s, max_retries, 0
        b, p, self.key_header = OHLCV[preset]
        self.base, self.path, self.key = base or b, path or p, api_key
        self._http, self.min_interval, self._clock, self._sleep, self._last = http or http_json, min_interval_s, clock, sleep, None

    RETRY = (429, 502, 503, 504)

    def _throttle(self):
        if self._last is not None:
            wait = self.min_interval - (self._clock() - self._last)
            if wait > 0:
                self._sleep(wait)
        self._last = self._clock()

    def _get(self, url, headers):
        """One request, retried with growing pauses on 429/5xx. A 429 also slows every later request:
        it means we were too fast, so the pace is permanently increased (x1.5, capped at 20 s)."""
        wait = self.backoff_s
        for attempt in range(self.max_retries + 1):
            self._throttle()
            try:
                st, body = self._http("GET", url, headers=headers)
            except Exception as e:
                return f"exc:{type(e).__name__}", None
            if st not in self.RETRY:
                return st, body
            if st == 429:
                self.throttled += 1
                self.min_interval = min(self.min_interval * 1.5, 20.0)
            if attempt < self.max_retries:
                self._sleep(wait)
                wait = min(wait * 2, 300)
        return st, None

    def fetch_pool(self, pool, network="solana", since_ts=None, until_ts=None, limit=1000, max_pages=60):
        """-> (status, rows). status: ok | empty | partial (error after some rows) | error:<code>.
        Pages backwards; a page shorter than `limit` means the history is exhausted (no confirming call)."""
        headers = {self.key_header: self.key} if (self.key_header and self.key) else {}
        before = int(until_ts if until_ts is not None else self._clock())
        got = {}
        for _ in range(max_pages):
            url = (self.base + self.path.format(network=network, pool=pool, tf="minute") +
                   f"?aggregate=1&limit={limit}&currency=usd&include_empty_intervals=false&before_timestamp={before}")
            st, body = self._get(url, headers)
            if st != 200:
                return (("partial" if got else f"error:{st}"), [got[k] for k in sorted(got)])
            rows = parse_ohlcv(body)
            if not rows:
                break
            for r in rows:
                got[r[0]] = r
            oldest = rows[0][0]
            if len(rows) < limit or (since_ts is not None and oldest <= since_ts) or oldest - 1 >= before:
                break
            before = int(oldest) - 1
        out = [got[k] for k in sorted(got) if since_ts is None or got[k][0] >= since_ts]
        return ("ok" if out else "empty"), out


def _iso_ts(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()
    except ValueError:
        return None


def parse_new_pools_full(body, network="solana"):
    """GeckoTerminal new_pools -> [{token, pool, symbol, created_ts, liq_usd}], ids/fields validated."""
    from discovery import B58
    out = []
    data = body.get("data") if isinstance(body, dict) else None
    for p in data if isinstance(data, list) else []:
        try:
            tid = p["relationships"]["base_token"]["data"]["id"]
            a = p["attributes"]
            pool = a["address"]
        except (KeyError, TypeError):
            continue
        if not (isinstance(tid, str) and tid.startswith(network + "_") and isinstance(pool, str) and B58.match(pool)):
            continue
        token = tid[len(network) + 1:]
        if not B58.match(token):
            continue
        liq = a.get("reserve_in_usd")
        try:
            liq = float(liq)
        except (TypeError, ValueError):
            liq = ""
        out.append({"token": token, "pool": pool, "symbol": str(a.get("name", "?")).split(" /")[0][:20],
                    "created_ts": _iso_ts(a.get("pool_created_at")) or "", "liq_usd": liq})
    return out


def discover(out_path, minutes, preset, key, interval=60, http=None, sleep=time.sleep, clock=time.time, pages=1, page_delay=2.5):
    """Poll new_pools. Always makes at least one pass (minutes=0 => a single pass), reading `pages` pages per pass."""
    base, path, hdr = DISCOVERY_PRESETS[preset]
    headers = {hdr: key} if (hdr and key) else {}
    seen = {}
    if os.path.exists(out_path):
        with open(out_path, newline="") as f:
            seen = {r["pool"]: r for r in csv.DictReader(f)}
    end = clock() + minutes * 60
    while True:
        for page in range(1, pages + 1):
            for attempt in range(4):
                try:
                    st, body = (http or http_json)("GET", f"{base}{path.format(network='solana')}?page={page}&include=base_token", headers=headers)
                except Exception:
                    st, body = 0, None
                if st != 429:
                    break
                sleep(20 * 2 ** attempt)                       # rate limited: wait, then retry this page
            if st != 200:
                print(f"[discover] page {page} failed with status {st}; stopping this pass")
                break
            fresh = parse_new_pools_full(body)
            for r in fresh:
                if r["pool"] not in seen:
                    seen[r["pool"]] = {**r, "seen_ts": int(clock())}
            if not fresh:
                break
            if page < pages:
                sleep(page_delay)
        _write(out_path, seen.values(), ["token", "pool", "symbol", "created_ts", "liq_usd", "seen_ts"])
        if clock() >= end:
            break
        sleep(interval)
    return seen


def _write(path, rows, cols):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"                                   # write-then-rename: a killed job never leaves a half file
    with open(tmp, "w", newline="") as f:
        w = csv.DictWriter(f, cols, extrasaction="ignore"); w.writeheader(); w.writerows(rows)
    os.replace(tmp, path)


POOL_COLS = ["token", "pool", "symbol", "created_ts", "liq_usd", "supply", "status"]
CANDLE_COLS = "token,symbol,ts,open,high,low,close,volume".split(",")


def _save(out_dir, meta, rows_by_token):
    cpath = os.path.join(out_dir, "candles.csv")
    with open(cpath + ".tmp", "w", newline="") as f:
        w = csv.writer(f); w.writerow(CANDLE_COLS)
        for rows in rows_by_token.values():
            w.writerows(rows)
    os.replace(cpath + ".tmp", cpath)
    _write(os.path.join(out_dir, "pools.csv"), meta.values(), POOL_COLS)


def fetch_all(pools_csv, out_dir, days, client, min_age_h=2.0, now=None, min_liq_usd=0.0, resume=True, checkpoint_every=20, log=print):
    """Fetch candles for every pool seen >= min_age_h ago. RESUMABLE: pools already `ok`/`empty` in out_dir are kept,
    pools that failed (error:*, partial) are retried, and progress is saved every `checkpoint_every` pools so a
    disconnect or a rate-limit storm does not lose finished work."""
    now = now or time.time()
    with open(pools_csv, newline="") as f:
        pools = [r for r in csv.DictReader(f)]
    os.makedirs(out_dir, exist_ok=True)
    meta, rows_by_token = {}, {}
    if resume and os.path.exists(os.path.join(out_dir, "pools.csv")):
        with open(os.path.join(out_dir, "pools.csv"), newline="") as f:
            meta = {r["pool"]: r for r in csv.DictReader(f)}
        cpath = os.path.join(out_dir, "candles.csv")
        if os.path.exists(cpath):
            with open(cpath, newline="") as f:
                rd = csv.reader(f); next(rd, None)
                for r in rd:
                    rows_by_token.setdefault(r[0], []).append(r)
    todo = [p for p in pools if now - float(p.get("seen_ts") or 0) >= min_age_h * 3600
            and float(p.get("liq_usd") or 0) >= min_liq_usd
            and meta.get(p["pool"], {}).get("status") not in ("ok", "empty")]
    too_young = [p for p in pools if now - float(p.get("seen_ts") or 0) < min_age_h * 3600]
    done_before = sum(1 for p in pools if meta.get(p["pool"], {}).get("status") in ("ok", "empty"))
    for n, p in enumerate(todo, 1):
        status, rows = client.fetch_pool(p["pool"], since_ts=now - days * 86400)
        rows_by_token.pop(p["token"], None)                      # a retry replaces any earlier partial rows
        if rows:
            rows_by_token[p["token"]] = [[p["token"], p["symbol"], *r] for r in rows]
        meta[p["pool"]] = {**p, "status": status}
        if n % checkpoint_every == 0:
            _save(out_dir, meta, rows_by_token)
            log(f"[fetch] {n}/{len(todo)} pools, {sum(len(v) for v in rows_by_token.values())} candles, "
                f"{getattr(client, 'throttled', 0)} rate-limit hits")
    _save(out_dir, meta, rows_by_token)
    counts = {}
    for m in meta.values():
        counts[m["status"]] = counts.get(m["status"], 0) + 1
    return {"pools": len(meta), "candles": sum(len(v) for v in rows_by_token.values()),
            "fetched_now": len(todo), "too_young_skipped": len(too_young), "already_done": done_before, "status": counts}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("discover"); d.add_argument("--out", required=True); d.add_argument("--minutes", type=float, default=60, help="0 = a single pass")
    d.add_argument("--pages", type=int, default=1, help="pages of ~20 pools per pass (the endpoint lists pools from roughly the last 48h)")
    f = sub.add_parser("fetch"); f.add_argument("--pools", required=True); f.add_argument("--out", required=True)
    f.add_argument("--days", type=float, default=14)
    f.add_argument("--min-liq-usd", type=float, default=0.0, help="skip pools whose liquidity AT DISCOVERY was below this (known at the time, so no lookahead)")
    f.add_argument("--min-age-h", type=float, default=2.0, help="skip pools seen more recently than this; 0 = fetch everything now")
    for s in (d, f):
        s.add_argument("--preset", choices=sorted(OHLCV), default="pro"); s.add_argument("--key")
    a = ap.parse_args()
    if a.cmd == "discover":
        print(f"{len(discover(a.out, a.minutes, a.preset, a.key, pages=a.pages))} pools in {a.out}")
    else:
        r = fetch_all(a.pools, a.out, a.days, GeckoOHLCV(a.preset, a.key), min_age_h=a.min_age_h, min_liq_usd=a.min_liq_usd)
        print(r)
        if r["too_young_skipped"] and not r["fetched_now"]:
            print(f"NOTHING DOWNLOADED: all {r['too_young_skipped']} pools were collected less than {a.min_age_h:g} hours ago. "
                  "Wait until they are old enough, then run this cell again.")
