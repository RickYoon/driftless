#!/usr/bin/env bash
# Start Driftless locally: http://127.0.0.1:8790
set -e
cd "$(dirname "$0")"
command -v claude >/dev/null || echo "note: 'claude' CLI not found — PRs will use the rule-based patch"
exec python3 server.py
