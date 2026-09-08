#!/usr/bin/env bash
# Click-through + API verify for Voice.app.
# Usage:
#   ./macos/Voice/scripts/verify.sh
#   ./macos/Voice/scripts/verify.sh --cold
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$ROOT"
if [[ -x /Users/jack/Documents/chatbot/.venv/bin/python ]]; then
  PYTHON=/Users/jack/Documents/chatbot/.venv/bin/python
elif [[ -x "$ROOT/.venv/bin/python" ]]; then
  PYTHON="$ROOT/.venv/bin/python"
else
  PYTHON="${PYTHON:-python3}"
fi
exec "$PYTHON" "$ROOT/scripts/verify-voice.py" --app "$ROOT/macos/Voice/build/Voice.app" "$@"
