#!/usr/bin/env bash
# Tear down a verification instance. Proof artifacts are kept.
set -euo pipefail

STATE_FILE="${1:-${VERIFY_STATE_FILE:-}}"
KEEP_DATA="${VERIFY_KEEP_DATA:-0}"

if [[ -z "$STATE_FILE" || ! -f "$STATE_FILE" ]]; then
  echo "Usage: cleanup.sh /tmp/chatbot-verify-<run-id>/state.env" >&2
  exit 2
fi

# shellcheck disable=SC1090
source "$STATE_FILE"

WEB_PID="${WEB_PID:-}"
SERVER_PID="${SERVER_PID:-}"

for pid in "$WEB_PID" "$SERVER_PID"; do
  [[ -n "$pid" ]] || continue
  if kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null || true
    for _ in $(seq 1 20); do
      kill -0 "$pid" 2>/dev/null || break
      sleep 0.5
    done
    if kill -0 "$pid" 2>/dev/null; then
      kill -9 "$pid" 2>/dev/null || true
    fi
  fi
done

for port in "$WEB_PORT" "$VOICE_PORT"; do
  pid="$(lsof -ti "TCP:$port" -sTCP:LISTEN 2>/dev/null | head -1 || true)"
  if [[ -n "$pid" ]]; then
    kill "$pid" 2>/dev/null || true
  fi
done

if [[ "$KEEP_DATA" != "1" && -d "$DATA_DIR" ]]; then
  rm -rf "$DATA_DIR"
fi

if [[ -d "$ARTIFACTS_DIR" ]]; then
  echo "artifacts_kept=$ARTIFACTS_DIR"
else
  echo "artifacts_kept=none"
fi

echo "cleaned run=$RUN_ID"
