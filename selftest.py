#!/usr/bin/env python3
"""End-to-end check: publish breaking changes -> PR opens (tests red->green) -> merge -> unfixable change -> issue, no PR.
Run: python3 selftest.py   (starts its own server on port 8791)"""
import copy, json, os, subprocess, sys, time, urllib.request

PORT = 8791
B = f"http://127.0.0.1:{PORT}"
HERE = os.path.dirname(os.path.abspath(__file__))


def get(p):
    return json.loads(urllib.request.urlopen(B + p).read())


def post(p, b):
    r = urllib.request.Request(B + p, data=json.dumps(b).encode(), method="POST", headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(r).read())


def publish(mutate):
    s = copy.deepcopy(get("/api/state")["spec"])
    mutate(s)
    post("/api/publish", {"spec": s})
    time.sleep(0.6)
    for _ in range(100):
        st = get("/api/state")
        if not st["busy"]:
            return st
        time.sleep(0.2)
    raise SystemExit("pipeline timed out")


def main():
    srv = subprocess.Popen([sys.executable, "server.py"], cwd=HERE, env={**os.environ, "PORT": str(PORT), "DRIFTLESS_AI": "0", "DRIFTLESS_JEV": "0"},
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(1.5)

        def v2(s):
            for e in s["endpoints"].values():
                e["path"] = e["path"].replace("/v1/charges", "/v2/payments")
                e["response"]["state"] = e["response"].pop("status")
            s["endpoints"]["create_charge"]["request"]["idempotency_key"] = {
                "type": "str", "required": True, "canon": "idem", "x-migration-default": "str(uuid.uuid4())"}

        st = publish(v2)
        pr = st["prs"][-1]
        assert pr["state"] == "open", pr
        assert pr["before"].startswith("FAILED") and pr["after"] == "OK", (pr["before"], pr["after"])
        post("/api/merge", {"id": pr["id"]})
        st = get("/api/state")
        assert st["main_version"] == 2 and "/v2/payments" in st["code"] and 'r["state"]' in st["code"]
        print("PASS  breaking change -> PR (red -> green) -> merged")

        st = publish(lambda s: s["endpoints"]["get_charge"]["response"].pop("state"))
        assert len(st["prs"]) == 1 and st["issues"], "expected an issue and no new PR"
        print("PASS  unfixable change -> issue, no PR")

        post("/api/subscribe", {"on": False})
        st = publish(lambda s: s["endpoints"]["create_charge"].update(path="/v9/payments"))
        assert any("not subscribed" in e["msg"] for e in st["events"][-3:])
        print("PASS  unsubscribed repo -> detected, no action")
    finally:
        srv.terminate()


if __name__ == "__main__":
    main()
