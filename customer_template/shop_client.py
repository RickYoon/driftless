"""acme-shop — Paylite API client (written against Paylite v1)."""
import json
import os
import urllib.request

BASE = os.environ.get("PAYLITE_BASE", "http://127.0.0.1:8790/vendor")


def _call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def create_charge(amount, currency):
    r = _call("POST", "/v1/charges", {"amount": amount, "currency": currency})
    return r["id"], r["status"]


def charge_status(charge_id):
    r = _call("GET", f"/v1/charges/{charge_id}")
    return r["status"]
