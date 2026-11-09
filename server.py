#!/usr/bin/env python3
"""Driftless — when a third-party API changes, your code keeps up.

One process, standard library only:
  /vendor/*        fake third-party API "Paylite" (behaves exactly as the current spec says)
  /hooks/paylite   webhook Paylite calls when it publishes a new spec version
  /api/*           Driftless dashboard API
  /                static site (landing + dashboard)

Pipeline per published version:
  webhook -> diff (rule-based) -> scan customer repo (AST) -> tests before
  -> scoped codemod -> range guard -> tests after -> branch + PR, or issue if it can't be verified.
Run: python3 server.py   ->  http://127.0.0.1:8790
"""
import ast
import copy
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(HERE, "web")
TPL = os.path.join(HERE, "customer_template")
STATE = os.path.join(HERE, ".state")
REPO = os.path.join(STATE, "acme-shop")
CLIENT = "shop_client.py"
PORT = int(os.environ.get("PORT", 8790))
ORIGIN = f"http://127.0.0.1:{PORT}"
BASE = ORIGIN + "/vendor"

SPEC0 = {
    "name": "Paylite API",
    "version": 1,
    "endpoints": {
        "create_charge": {
            "method": "POST", "path": "/v1/charges",
            "request": {
                "amount": {"type": "int", "required": True, "canon": "amount"},
                "currency": {"type": "str", "required": True, "canon": "currency"},
            },
            "response": {
                "id": {"type": "str", "canon": "id"},
                "amount": {"type": "int", "canon": "amount"},
                "status": {"type": "str", "canon": "status"},
            },
        },
        "get_charge": {
            "method": "GET", "path": "/v1/charges/{id}",
            "request": {},
            "response": {
                "id": {"type": "str", "canon": "id"},
                "status": {"type": "str", "canon": "status"},
            },
        },
    },
}

S = {"spec": None, "main_spec": None, "connected": False, "repo": "", "api": "", "lang": "en", "events": [], "prs": [], "issues": [], "subscribed": True, "vendor_errors": [], "busy": False}
RUN_LOCK = threading.Lock()


# ---------------------------------------------------------------- helpers
def ev(kind, msg, **kw):
    S["events"].append({"t": time.strftime("%H:%M:%S"), "kind": kind, "msg": msg, **kw})


def git(*args, cwd=REPO):
    return subprocess.run(["git", "-c", "user.name=driftless-bot", "-c", "user.email=bot@driftless.local", *args],
                          cwd=cwd, capture_output=True, text=True)


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def reset():
    shutil.rmtree(STATE, ignore_errors=True)
    os.makedirs(REPO)
    for f in os.listdir(TPL):
        if f.endswith(".py"):
            shutil.copy(os.path.join(TPL, f), REPO)
    git("init", "-q", "-b", "main")
    git("add", ".")
    git("commit", "-qm", "acme-shop: checkout written against Paylite v1")
    S.update(connected=False, repo="", api="", spec=copy.deepcopy(SPEC0), main_spec=copy.deepcopy(SPEC0), events=[], prs=[], issues=[], subscribed=True, vendor_errors=[], busy=False)
    ev("info", "acme-shop connected (read-only + PR scope). Subscribed to Paylite API updates.")


# ---------------------------------------------------------------- fake vendor
def vendor(method, path, body):
    for name, e in S["spec"]["endpoints"].items():
        if e["method"] != method:
            continue
        m = re.match("^" + re.sub(r"\{[^}]+\}", r"([^/]+)", e["path"]) + "$", path)
        if not m:
            continue
        req = e.get("request", {})
        if method == "POST":
            for k, v in req.items():
                if v.get("required") and k not in body:
                    return 400, {"error": f"missing required field '{k}'"}
            for k in body:
                if k not in req:
                    return 400, {"error": f"unknown field '{k}'"}
        by_canon = {req[k]["canon"]: body[k] for k in body if k in req}
        vals = {"id": m.groups()[0] if m.groups() else "ch_123", "status": "succeeded",
                "amount": by_canon.get("amount", 1000), "currency": by_canon.get("currency", "usd")}
        return 200, {k: vals.get(v["canon"], "") for k, v in e["response"].items()}
    return 404, {"error": f"no such endpoint {method} {path}"}


# ---------------------------------------------------------------- 1. diff (rules, not LLM)
def diff_specs(a, b):
    out = []
    for n, ea in a["endpoints"].items():
        eb = b["endpoints"].get(n)
        if not eb:
            out.append({"op": "remove_endpoint", "ep": n, "breaking": True, "desc": f"{n}: endpoint removed"})
            continue
        if ea["path"] != eb["path"]:
            out.append({"op": "rename_path", "ep": n, "old": ea["path"], "new": eb["path"], "breaking": True,
                        "desc": f"{n}: {ea['method']} {ea['path']} → {eb['path']}"})
        for where in ("request", "response"):
            fa, fb = ea.get(where, {}), eb.get(where, {})
            ca = {v["canon"]: k for k, v in fa.items()}
            cb = {v["canon"]: k for k, v in fb.items()}
            for c, k in ca.items():
                if c in cb and cb[c] != k:
                    out.append({"op": "rename_field", "ep": n, "where": where, "old": k, "new": cb[c], "breaking": True,
                                "desc": f"{n}: {where} field '{k}' → '{cb[c]}'"})
                elif c not in cb:
                    out.append({"op": "remove_field", "ep": n, "where": where, "old": k, "breaking": True,
                                "desc": f"{n}: {where} field '{k}' removed"})
            for c, k in cb.items():
                if c not in ca:
                    v = fb[k]
                    if where == "request" and v.get("required"):
                        out.append({"op": "add_required", "ep": n, "field": k, "default": v.get("x-migration-default"),
                                    "breaking": True, "desc": f"{n}: new required request field '{k}'"})
                    else:
                        out.append({"op": "add_field", "ep": n, "where": where, "field": k, "breaking": False,
                                    "desc": f"{n}: new optional {where} field '{k}'"})
    return out


# ---------------------------------------------------------------- 2. scan (AST)
def static_prefix(path):
    return path.split("{")[0]


def scan(src, spec_old, changes):
    tree = ast.parse(src)
    lines = src.splitlines()
    funcs = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
    hits = {}
    for ch in changes:
        e = spec_old["endpoints"].get(ch["ep"])
        if not e:
            continue
        pre = static_prefix(e["path"])
        for f in funcs:
            body = "\n".join(lines[f.lineno - 1:f.end_lineno])
            literal_hit = re.search(r"[\"']" + re.escape(pre) + (r"[\"']" if pre == e["path"] else ""), body)
            if literal_hit and f'"{e["method"]}"' in body:
                hits.setdefault(ch["ep"], {"fn": f.name, "start": f.lineno, "end": f.end_lineno})
    return hits


# ---------------------------------------------------------------- 3. patch (scoped codemod)
def patch(src, spec_old, changes, hits):
    lines = src.splitlines()
    unfixable, applied = [], []
    need_uuid = False
    for ch in changes:
        h = hits.get(ch["ep"])
        if not h:
            continue
        rng = range(h["start"] - 1, h["end"])
        if ch["op"] == "rename_path":
            old_p, new_p = static_prefix(ch["old"]), static_prefix(ch["new"])
            for i in rng:
                lines[i] = lines[i].replace(f'"{old_p}', f'"{new_p}')
            applied.append(ch)
        elif ch["op"] == "rename_field" and ch["where"] == "request":
            pat = re.compile(r"([\"'])" + re.escape(ch["old"]) + r"\1(\s*:)")
            for i in rng:
                lines[i] = pat.sub(lambda m: f'"{ch["new"]}"{m.group(2)}', lines[i])
            applied.append(ch)
        elif ch["op"] == "rename_field" and ch["where"] == "response":
            pat = re.compile(r"\[([\"'])" + re.escape(ch["old"]) + r"\1\]")
            for i in rng:
                lines[i] = pat.sub(f'["{ch["new"]}"]', lines[i])
            applied.append(ch)
        elif ch["op"] == "add_required":
            if not ch.get("default"):
                unfixable.append({**ch, "why": "vendor gave no migration default; a human must choose a value"})
                continue
            done = False
            for i in rng:
                if "_call(" in lines[i] and "{" in lines[i] and not done:
                    lines[i] = lines[i].replace("{", '{' + f'"{ch["field"]}": {ch["default"]}, ', 1)
                    done = True
            if done:
                applied.append(ch)
                need_uuid = need_uuid or "uuid." in ch["default"]
            else:
                unfixable.append({**ch, "why": "no request body found to extend"})
        elif ch["op"] == "remove_field" and ch["where"] == "response":
            used = any(re.search(r"\[[\"']" + re.escape(ch["old"]) + r"[\"']\]", lines[i]) for i in rng)
            if used:
                unfixable.append({**ch, "why": f"code reads '{ch['old']}', which no longer exists; there is no safe automatic replacement"})
        elif ch["op"] == "remove_endpoint":
            unfixable.append({**ch, "why": "endpoint removed; needs a product decision"})
    if need_uuid and not re.search(r"^import uuid", "\n".join(lines), re.M):
        idx = max(i for i, l in enumerate(lines) if l.startswith("import ")) + 1
        lines.insert(idx, "import uuid")
    return "\n".join(lines) + "\n", applied, unfixable


# ---------------------------------------------------------------- 3b. AI patch (Claude writes code + PR, Driftless verifies)
def ai_patch(src, old_spec, new_spec, changes, lang):
    if os.environ.get("DRIFTLESS_AI") == "0":
        raise RuntimeError("AI disabled")
    lang_line = "Write TITLE and BODY in Korean." if lang == "ko" else "Write TITLE and BODY in English."
    prompt = (
        "You are Driftless, a bot that keeps a customer's code compatible with a third-party API.\n"
        f"The API 'Paylite' changed from v{old_spec['version']} to v{new_spec['version']}. Breaking changes:\n"
        + "\n".join("- " + c["desc"] for c in changes)
        + "\n\nNew Paylite spec (JSON):\n" + json.dumps(new_spec, indent=1)
        + "\n\nCustomer file shop_client.py:\n```python\n" + src + "```\n\n"
        "Rules: change only what the new API requires. Keep function names and signatures. Do not touch unrelated lines. "
        "For a new required request field, use the provider's x-migration-default expression and add any import it needs.\n"
        f"{lang_line} Keep BODY under 120 words: what changed in the API, what you changed in the code, and that tests will verify it.\n"
        "Respond in exactly this format and nothing else:\nTITLE: <one line>\nBODY:\n<markdown>\nFILE:\n```python\n<entire updated shop_client.py>\n```\n")
    p = subprocess.run(["claude", "-p", prompt, "--model", "sonnet"], capture_output=True, text=True, timeout=150, cwd=STATE)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or "claude CLI failed").strip()[:200])
    out = p.stdout
    t = re.search(r"^TITLE:\s*(.+)$", out, re.M)
    b = re.search(r"BODY:\s*\n(.*?)\nFILE:", out, re.S)
    f = re.search(r"FILE:\s*\n```(?:python)?\n(.*?)```", out, re.S)
    if not (t and b and f):
        raise RuntimeError("could not parse AI output")
    code = f.group(1)
    if not code.endswith("\n"):
        code += "\n"
    return code, t.group(1).strip(), b.group(1).strip()


def range_guard(old, new, hits):
    allowed = set()
    for h in hits.values():
        allowed.update(range(h["start"] - 1, h["end"]))
    sm = difflib.SequenceMatcher(None, old.splitlines(), new.splitlines())
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        new_chunk = new.splitlines()[j1:j2]
        if tag == "insert" and all(l.startswith("import ") for l in new_chunk):
            continue
        if not all(i in allowed for i in range(i1, max(i2, i1 + 1))):
            return False
    return True


# ---------------------------------------------------------------- 4. verify
def run_tests(cwd):
    n0 = len(S["vendor_errors"])
    p = subprocess.run([sys.executable, "-m", "unittest", "-q"], cwd=cwd, capture_output=True, text=True,
                       timeout=60, env={**os.environ, "PAYLITE_BASE": BASE})
    out = (p.stderr or p.stdout).strip().splitlines()
    summary = next((l for l in reversed(out) if l.startswith(("OK", "FAILED"))), out[-1] if out else "")
    errs = list(dict.fromkeys(S["vendor_errors"][n0:]))
    return p.returncode == 0, summary, errs


# ---------------------------------------------------------------- pipeline
def pipeline(old_spec, new_spec):
    with RUN_LOCK:
        S["busy"] = True
        try:
            _pipeline(old_spec, new_spec)
        except Exception as e:  # fail loudly
            ev("error", f"pipeline crashed: {e}")
        finally:
            S["busy"] = False


def _pipeline(old_spec, new_spec):
    v0, v1 = old_spec["version"], new_spec["version"]
    ev("hook", f"Webhook from Paylite: v{v1} published (acme-shop main is written for v{v0})")
    changes = diff_specs(old_spec, new_spec)
    if not changes:
        ev("info", "Spec diff: no contract changes. Nothing to do.")
        return
    ev("diff", f"Spec diff: {len(changes)} change(s)", items=[c["desc"] + (" · breaking" if c["breaking"] else "") for c in changes])
    if not S["subscribed"]:
        ev("warn", "acme-shop is not subscribed. Change detected, no action taken.")
        return
    breaking = [c for c in changes if c["breaking"]]
    if not breaking:
        ev("info", "Only non-breaking changes. No PR needed.")
        return
    src = read(os.path.join(REPO, CLIENT))
    hits = scan(src, old_spec, breaking)
    if not hits:
        ev("info", "Scanned acme-shop: no affected call sites. Nothing to do.")
        return
    ev("scan", f"Scanned acme-shop: {len(hits)} affected call site(s)",
       items=[f"{CLIENT}:{h['start']}–{h['end']}  {h['fn']}()  uses {ep}" for ep, h in hits.items()])
    ok0, sum0, err0 = run_tests(REPO)
    ev("test", ("Tests on main against live Paylite: " + ("passing" if ok0 else "FAILING")) + f"  ({sum0})",
       ok=ok0, items=[f"Paylite said: {e}" for e in err0])
    rule_src, applied, unfixable = patch(src, old_spec, breaking, hits)
    new_src, engine, title, body = rule_src, "rules", f"Adapt to Paylite API v{v1}", ""
    if unfixable:
        issue = {"id": len(S["issues"]) + 1, "title": f"Paylite v{v1} needs a human decision",
                 "items": [f"{u['desc']}: {u['why']}" for u in unfixable], "t": time.strftime("%H:%M:%S")}
        S["issues"].append(issue)
        ev("issue", f"Cannot verify a safe fix. Opened issue #{issue['id']} instead of a PR.", items=issue["items"])
        return
    ev("ai", "Claude is writing the fix and the PR description…")
    try:
        ai_src, ai_title, ai_body = ai_patch(src, old_spec, new_spec, breaking, S["lang"])
        if ai_src.strip() == src.strip():
            raise RuntimeError("AI returned no change")
        if range_guard(src, ai_src, hits):
            new_src, engine, title, body = ai_src, "claude", ai_title, ai_body
            ev("ai", "Claude wrote the patch and PR description.")
        else:
            ev("warn", "AI patch touched lines outside the affected functions. Falling back to the rule-based patch.")
    except Exception as e:
        ev("warn", f"AI unavailable ({e}). Falling back to the rule-based patch.")
    if new_src == src:
        ev("info", "No code change required.")
        return
    if not range_guard(src, new_src, hits):
        ev("error", "Range guard: patch touched lines outside the affected functions. Rejected.")
        return
    ev("patch", f"Scoped patch ready ({'Claude' if engine == 'claude' else 'rules'}): only inside affected functions. Range guard passed.")
    tmp = tempfile.mkdtemp(prefix="driftless-")
    for f in os.listdir(REPO):
        if f.endswith(".py"):
            shutil.copy(os.path.join(REPO, f), tmp)
    with open(os.path.join(tmp, CLIENT), "w", encoding="utf-8") as fh:
        fh.write(new_src)
    ok1, sum1, err1 = run_tests(tmp)
    shutil.rmtree(tmp, ignore_errors=True)
    ev("test", "Tests with patch against Paylite v%d: %s  (%s)" % (v1, "passing" if ok1 else "FAILING", sum1),
       ok=ok1, items=[f"Paylite said: {e}" for e in err1])
    if not ok1:
        issue = {"id": len(S["issues"]) + 1, "title": f"Paylite v{v1}: automatic patch failed verification",
                 "items": [sum1] + err1, "t": time.strftime("%H:%M:%S")}
        S["issues"].append(issue)
        ev("issue", f"Patch did not pass tests. No PR. Opened issue #{issue['id']}.")
        return
    branch = f"driftless/paylite-v{v1}"
    git("checkout", "-q", "-b", branch)
    with open(os.path.join(REPO, CLIENT), "w", encoding="utf-8") as fh:
        fh.write(new_src)
    git("add", CLIENT)
    git("commit", "-qm", title)
    git("checkout", "-q", "main")
    d = "".join(difflib.unified_diff(src.splitlines(True), new_src.splitlines(True), f"a/{CLIENT}", f"b/{CLIENT}"))
    pr = {"id": len(S["prs"]) + 1, "title": title, "body": body, "engine": engine, "branch": branch, "diff": d,
          "changes": [c["desc"] for c in applied], "before": sum0, "after": sum1, "state": "open",
          "t": time.strftime("%H:%M:%S"), "spec": copy.deepcopy(new_spec)}
    for p in S["prs"]:
        if p["state"] == "open":
            p["state"] = "superseded"
            git("branch", "-q", "-D", p["branch"])
    S["prs"].append(pr)
    ev("pr", f"Opened PR #{pr['id']}: {pr['title']} — tests red → green. Waiting for your review.")


# ---------------------------------------------------------------- http
class H(SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def _json(self, code, obj):
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        if self.path.startswith("/vendor/"):
            code, out = vendor("GET", self.path[len("/vendor"):], {})
            if code != 200:
                S["vendor_errors"].append(f"{code} {out['error']}")
            return self._json(code, out)
        if self.path == "/api/state":
            prs = [{k: v for k, v in p.items() if k != "spec"} for p in S["prs"]]
            return self._json(200, {"spec": S["spec"], "main_version": S["main_spec"]["version"], "events": S["events"], "prs": prs, "issues": S["issues"],
                                    "subscribed": S["subscribed"], "busy": S["busy"], "connected": S["connected"], "repo": S["repo"], "api": S["api"],
                                    "code": read(os.path.join(REPO, CLIENT))})
        return super().do_GET()

    def do_POST(self):
        try:
            body = self._body()
        except Exception:
            return self._json(400, {"error": "invalid JSON"})
        if self.path.startswith("/vendor/"):
            code, out = vendor("POST", self.path[len("/vendor"):], body)
            if code != 200:
                S["vendor_errors"].append(f"{code} {out['error']}")
            return self._json(code, out)
        if self.path == "/api/publish":
            new = body.get("spec")
            if not isinstance(new, dict) or "endpoints" not in new:
                return self._json(400, {"error": "spec must be an object with endpoints"})
            if S["busy"]:
                return self._json(409, {"error": "pipeline still running"})
            new = copy.deepcopy(new)
            new["version"] = S["spec"]["version"] + 1
            S["spec"] = new
            threading.Thread(target=_fire_webhook, args=(copy.deepcopy(S["main_spec"]), new), daemon=True).start()
            return self._json(200, {"ok": True, "version": new["version"]})
        if self.path == "/api/connect":
            repo = (body.get("repo") or "").strip()
            if not repo:
                return self._json(400, {"error": "repo is required"})
            S.update(connected=True, subscribed=True, repo=repo, api=body.get("api") or "Paylite API",
                     lang="ko" if body.get("lang") == "ko" else "en")
            ev("info", f"Connected {repo}. Watching {S['api']} for changes.")
            return self._json(200, {"ok": True})
        if self.path == "/api/demo-change":
            if S["busy"]:
                return self._json(409, {"error": "pipeline still running"})
            if body.get("lang") in ("ko", "en"):
                S["lang"] = body["lang"]
            new = copy.deepcopy(S["spec"])
            for e in new["endpoints"].values():
                m = re.search(r"/v(\d+)/", e["path"])
                n = int(m.group(1)) + 1 if m else 2
                e["path"] = re.sub(r"/v\d+/(charges|payments)", f"/v{n}/payments", e["path"])
                r = e["response"]
                k = next((k for k, v in r.items() if v["canon"] == "status"), None)
                if k == "status":
                    r["state"] = r.pop("status")
            req = new["endpoints"]["create_charge"]["request"]
            if not any(v["canon"] == "idem" for v in req.values()):
                req["idempotency_key"] = {"type": "str", "required": True, "canon": "idem", "x-migration-default": "str(uuid.uuid4())"}
            new["version"] = S["spec"]["version"] + 1
            S["spec"] = new
            threading.Thread(target=_fire_webhook, args=(copy.deepcopy(S["main_spec"]), new), daemon=True).start()
            return self._json(200, {"ok": True, "version": new["version"]})
        if self.path == "/hooks/paylite":
            threading.Thread(target=pipeline, args=(body["old"], body["new"]), daemon=True).start()
            return self._json(202, {"accepted": True})
        if self.path == "/api/subscribe":
            S["subscribed"] = bool(body.get("on"))
            ev("info", "Subscription " + ("activated (Pro)." if S["subscribed"] else "paused."))
            return self._json(200, {"subscribed": S["subscribed"]})
        if self.path == "/api/merge":
            pr = next((p for p in S["prs"] if p["id"] == body.get("id")), None)
            if not pr or pr["state"] != "open":
                return self._json(400, {"error": "no open PR with that id"})
            r = git("merge", "-q", "--ff-only", pr["branch"])
            if r.returncode != 0:
                return self._json(409, {"error": r.stderr.strip() or "merge failed"})
            pr["state"] = "merged"
            S["main_spec"] = copy.deepcopy(pr["spec"])
            ev("merge", f"You merged PR #{pr['id']}. acme-shop main now speaks Paylite v{S['spec']['version']}.")
            return self._json(200, {"ok": True})
        if self.path == "/api/reset":
            if S["busy"]:
                return self._json(409, {"error": "pipeline still running"})
            reset()
            return self._json(200, {"ok": True})
        return self._json(404, {"error": "not found"})


def _fire_webhook(old, new):
    data = json.dumps({"old": old, "new": new}).encode()
    req = urllib.request.Request(ORIGIN + "/hooks/paylite", data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as e:
        ev("error", f"webhook delivery failed: {e}")


if __name__ == "__main__":
    reset()
    print(f"Driftless → {ORIGIN}   (dashboard: {ORIGIN}/app.html)")
    ThreadingHTTPServer(("127.0.0.1", PORT), partial(H, directory=WEB)).serve_forever()
