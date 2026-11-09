#!/usr/bin/env python3
"""Driftless MCP server (stdio). Lets an AI assistant talk to a running Driftless.

Add to Claude Code:  claude mcp add driftless -- python3 /path/to/selfmaint/mcp_server.py
Needs the Driftless server running (python3 server.py). Standard library only.
Merging is intentionally NOT exposed: only a human merges.
"""
import json
import os
import sys
import urllib.request

BASE = os.environ.get("DRIFTLESS_URL", "http://127.0.0.1:8790")


def http(path, body=None):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode() if body is not None else None,
                                 method="POST" if body is not None else "GET",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def t_status(_):
    s = http("/api/state")
    return {"api_spec_version": s["spec"]["version"], "repo_code_matches_version": s["main_version"],
            "pipeline_running": s["busy"], "sources": len(s["sources"]),
            "open_prs": [p["id"] for p in s["prs"] if p["state"] == "open"], "issues": len(s["issues"]),
            "alert_channels": s["config"]}


def t_sources(_):
    return [{k: x.get(k) for k in ("id", "type", "name", "url", "interval", "last_check", "status")}
            for x in http("/api/state")["sources"]]


def t_add_source(a):
    return http("/api/sources", {"type": a.get("type"), "url": a.get("url"), "name": a.get("name"),
                                 "interval": a.get("interval", 60)})


def t_check(a):
    return http("/api/check", {"id": a.get("id")})


def t_changes(a):
    n = int(a.get("limit", 10))
    ev = [e for e in http("/api/state")["events"] if e["kind"] in ("source", "diff", "pr", "issue", "merge")]
    return ev[-n:]


def t_prs(_):
    return [{k: p.get(k) for k in ("id", "state", "title", "engine", "body", "before", "after", "changes", "diff")}
            for p in http("/api/state")["prs"]]


def t_alerts(_):
    return http("/api/state")["outbox"]


TOOLS = {
    "driftless_status": (t_status, "Overall status: API spec version, which version the repo code matches, open PRs, alert channels.", {}),
    "list_sources": (t_sources, "List watched sources (API specs and crawled web pages) with last check time and status.", {}),
    "add_source": (t_add_source, "Start watching a new source. type is 'api' (OpenAPI JSON URL) or 'page' (web page to crawl). External sites are checked at most every 60s.",
                   {"type": {"type": "string", "enum": ["api", "page"]}, "url": {"type": "string"},
                    "name": {"type": "string"}, "interval": {"type": "integer", "description": "seconds"}}),
    "check_now": (t_check, "Check one source right now by id (see list_sources).", {"id": {"type": "string"}}),
    "recent_changes": (t_changes, "Recent detected changes: page diffs, API spec diffs, PRs, issues, merges.",
                       {"limit": {"type": "integer"}}),
    "list_prs": (t_prs, "Pull requests Driftless opened, including the AI-written description, test results before/after and the diff. Merging is human-only.", {}),
    "list_alerts": (t_alerts, "Alerts sent and their per-channel delivery status (Slack, email).", {}),
}


def schema(props):
    req = ["type", "url"] if "url" in props else (["id"] if "id" in props else [])
    return {"type": "object", "properties": props, "required": req}


def handle(msg):
    m, mid = msg.get("method"), msg.get("id")
    if m == "initialize":
        ver = (msg.get("params") or {}).get("protocolVersion", "2025-06-18")
        return {"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": ver, "capabilities": {"tools": {}},
                                                         "serverInfo": {"name": "driftless", "version": "0.1"}}}
    if m == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    if m == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": [
            {"name": n, "description": d, "inputSchema": schema(p)} for n, (_, d, p) in TOOLS.items()]}}
    if m == "tools/call":
        p = msg.get("params") or {}
        name = p.get("name")
        if name not in TOOLS:
            return {"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": f"unknown tool {name}"}], "isError": True}}
        try:
            out = TOOLS[name][0](p.get("arguments") or {})
            return {"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False, indent=1)}]}}
        except Exception as e:
            return {"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": f"Driftless error: {e} (is server.py running at {BASE}?)"}], "isError": True}}
    if mid is None:  # notification
        return None
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {m}"}}


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            resp = handle(json.loads(line))
        except Exception as e:
            resp = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": str(e)}}
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
