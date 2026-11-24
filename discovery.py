"""Extra token-discovery sources, and a coverage report.

DexScreener's /token-profiles/latest only lists tokens whose teams registered a profile,
so it misses most new launches. GeckoTerminal's new-pools endpoint lists newly created
pools network-wide (pools from the last 48h, 20 per page).

CONFIRMED from CoinGecko docs: path /onchain/networks/{network}/new_pools, params page and
include=base_token, Pro base https://pro-api.coingecko.com/api/v3 with header
x-cg-pro-api-key, response data[].attributes.{address,pool_created_at,...} and
data[].relationships.base_token.data.id.
NOT confirmed: that ids look like "solana_<mint>" (docs show eth_0x... examples), rate
limits, and whether the keyless "public" preset below works. verify_live.py checks them.

    python discovery.py --minutes 30 --interval 30 [--gecko-preset pro --gecko-key K] [--truth mints.txt]
"""
import argparse
import re
import time

from net import http_json

B58 = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
PRESETS = {
    "pro": ("https://pro-api.coingecko.com/api/v3", "/onchain/networks/{network}/new_pools", "x-cg-pro-api-key"),
    "public": ("https://api.geckoterminal.com/api/v2", "/networks/{network}/new_pools", None),
}


def parse_new_pools(body, network="solana"):
    mints = []
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, list):
        return mints
    for pool in data:
        try:
            tid = pool["relationships"]["base_token"]["data"]["id"]
        except (KeyError, TypeError):
            continue
        prefix = network + "_"
        if isinstance(tid, str) and tid.startswith(prefix):
            m = tid[len(prefix):]
            if B58.match(m) and m not in mints:
                mints.append(m)
    return mints


class GeckoNewPools:
    def __init__(self, network="solana", preset="pro", api_key=None, pages=1, http=None,
                 base=None, path=None):
        b, p, self.key_header = PRESETS[preset]
        self.base, self.path = base or b, path or p
        self.network, self.key, self.pages = network, api_key, pages
        self._http = http or http_json

    def candidates(self):
        out = []
        headers = {self.key_header: self.key} if (self.key_header and self.key) else {}
        for page in range(1, self.pages + 1):
            url = f"{self.base}{self.path.format(network=self.network)}?page={page}&include=base_token"
            try:
                status, body = self._http("GET", url, headers=headers)
            except Exception as e:
                print(f"[gecko] request failed: {e}")
                break
            if status != 200:
                print(f"[gecko] status {status}")
                break
            out.extend(m for m in parse_new_pools(body, self.network) if m not in out)
        return out


def coverage_report(sets, truth=None):
    """sets: {source: set(mints)}. With `truth` (e.g. an indexer export of all launches in
    the window) also reports recall. Without it, 'unique' is a LOWER BOUND on what each
    source adds, and the union's true coverage is unknown."""
    names = list(sets)
    union = set().union(*sets.values()) if sets else set()
    rep = {"union": len(union), "sources": {}}
    for n in names:
        others = set().union(*[sets[m] for m in names if m != n]) if len(names) > 1 else set()
        row = {"found": len(sets[n]), "unique": len(sets[n] - others),
               "overlap_with_others": len(sets[n] & others)}
        if truth is not None:
            row["recall"] = len(sets[n] & truth) / len(truth) if truth else None
        rep["sources"][n] = row
    if truth is not None:
        rep["union_recall"] = len(union & truth) / len(truth) if truth else None
    return rep


if __name__ == "__main__":
    from data import DexScreenerSource
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=30)
    ap.add_argument("--interval", type=float, default=30)
    ap.add_argument("--gecko-preset", choices=sorted(PRESETS), default="pro")
    ap.add_argument("--gecko-key")
    ap.add_argument("--truth", help="file with one mint per line (all launches in the window)")
    a = ap.parse_args()
    srcs = {"dexscreener_profiles": DexScreenerSource("solana"),
            "geckoterminal_new_pools": GeckoNewPools("solana", a.gecko_preset, a.gecko_key)}
    sets = {n: set() for n in srcs}
    end = time.time() + a.minutes * 60
    while time.time() < end:
        for n, s in srcs.items():
            sets[n].update(s.candidates())
        time.sleep(a.interval)
    truth = set(open(a.truth).read().split()) if a.truth else None
    import json
    print(json.dumps(coverage_report(sets, truth), indent=2))
