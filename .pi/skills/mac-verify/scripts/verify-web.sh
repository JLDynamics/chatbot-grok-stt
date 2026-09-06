#!/usr/bin/env bash
# Web sidecar verify: pytest + curl smoke.
# Usage: verify-web.sh [WEB_PORT]
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
cd "$HERE"
WEB_PORT="${1:-7860}"
uv run pytest tests/test_web_app_server.py -q
echo "--- API smoke :$WEB_PORT ---"
curl -sf "http://127.0.0.1:$WEB_PORT/api/health" \
  || curl -sf "http://127.0.0.1:$WEB_PORT/docs" -o /dev/null -w "docs OK %{http_code}\n"
