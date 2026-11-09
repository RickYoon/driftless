# Driftless — APIs drift. Your code doesn't.

**YC Request for Startups: Self-Maintaining APIs** · built at c0mpiled-17 Seoul (2026-11-09)

When a third-party API ships a breaking change, nobody reads the changelog until production breaks.
Driftless subscribes your repo to the APIs it depends on. When the provider publishes a new spec, Driftless:

1. **Detects** the change from a provider webhook and diffs the spec with rules (renamed fields, moved endpoints, new required params, removals). No LLM guessing at this step.
2. **Scans** your code (Python AST) and maps each change to the exact functions that use it. No impact, no noise.
3. **Writes the fix with AI**: Claude reads the spec change and your file, then writes the patch and the PR description. A range guard rejects any edit outside the affected functions (falls back to a rule-based patch).
4. **Proves** it: runs your tests against the live new API before and after the patch.
5. **Opens a PR** only when the patch turns tests from red to green. If a change has no safe fix (for example, a field you read was removed), it opens an **issue with the reason** instead of touching your code.

A human always merges.

## Demo

Paylite is a fictional payments API that runs inside the demo and behaves exactly as its current spec says.
In the dashboard you play the provider: ship a change, publish, and watch the customer repo (`acme-shop`) get a verified PR.

```bash
python3 server.py          # http://127.0.0.1:8790  (landing)  ·  /app.html  (dashboard)
python3 selftest.py        # end-to-end check on port 8791
```

Python 3.10+, git, and the `claude` CLI for AI-written PRs (optional; falls back to rules). Standard library only.

- `/app.html` — the user flow: repo + API → API changes → Claude writes the PR → verified → merge
- `/console.html` — every internal step, and you can play the API provider

## Use it from an AI assistant (MCP)

```bash
claude mcp add driftless -- python3 /path/to/selfmaint/mcp_server.py
```

Tools: `driftless_status`, `list_sources`, `add_source`, `check_now`, `recent_changes`, `list_prs`, `list_alerts`. Merging is not exposed. Verified: Claude answered "what changed in Paylite and what are the PR's test results" through these tools.

## What the self-test checks

- Breaking changes (endpoints move to `/v2/payments`, `status` → `state`, new required `idempotency_key`) → PR opens with tests `FAILED` → `OK`, then merges.
- A removed response field the code depends on → issue, no PR.
- Unsubscribed repo → change detected, no action.

## Security model

- Scope: read the repo, create branches and PRs. Never writes to `main`, never merges.
- Only the affected file is sent to the AI model (Claude, via the `claude` CLI). AI output is never trusted directly: range guard + your tests decide.
- Without AI (`DRIFTLESS_AI=0` or no CLI), a deterministic rule-based patch is used.
- Verification gate: no passing tests against the new API, no PR.

## Business model

Subscription per repo (Hobby free · Pro $20/repo/mo · Provider plans for API companies that want to ship an "update agent" to their customers). Beta pricing for the demo.

## Limits (honest)

- One language (Python) and one fictional provider. Patch rules cover renames, path moves, new required params with a provider-supplied migration default.
- Changes without a safe automatic fix are escalated as issues by design.
- The "PR" is a real git branch + commit in a local repo; GitHub App integration is the next step.
