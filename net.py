import json
import urllib.error
import urllib.request


def http_json(method, url, body=None, headers=None, timeout=10):
    """Returns (status, parsed_json_or_None). Network errors raise."""
    data = json.dumps(body).encode() if body is not None else None
    h = {"Content-Type": "application/json", "User-Agent": "paper-bot/0.4"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.load(e)
        except Exception:
            return e.code, None
