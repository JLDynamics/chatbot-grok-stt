#!/usr/bin/env bash
# Start an isolated Chatbot instance for verification.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/../../../.." && pwd)"

RUN_ID="${VERIFY_RUN_ID:-$(date +%Y%m%d-%H%M%S)-$$}"
STATE_DIR="${VERIFY_STATE_DIR:-/tmp/chatbot-verify-$RUN_ID}"
WEB_PORT="${VERIFY_WEB_PORT:-17860}"
VOICE_PORT="${VERIFY_VOICE_PORT:-18766}"
DATA_DIR="${CHATBOT_DATA_DIR:-/tmp/chatbot-verify-$RUN_ID/data}"
ARTIFACTS_DIR="${VERIFY_ARTIFACTS_DIR:-$ROOT/artifacts/verify-chatbot/runs/$RUN_ID}"

mkdir -p "$STATE_DIR" "$ARTIFACTS_DIR" "$DATA_DIR"

listener() { lsof -ti "TCP:$1" -sTCP:LISTEN 2>/dev/null | head -1 || true; }
for port in "$WEB_PORT" "$VOICE_PORT"; do
  occupant="$(listener "$port")"
  if [[ -n "$occupant" ]]; then
    echo "Error: port $port is already in use by pid $occupant." >&2
    echo "Set VERIFY_WEB_PORT / VERIFY_VOICE_PORT or run cleanup for the prior run." >&2
    exit 1
  fi
done

CHATBOT_ENV="$HOME/.config/chatbot/env"
if [[ -f "$CHATBOT_ENV" ]]; then
  # shellcheck disable=SC1090
  source "$CHATBOT_ENV"
fi
if [[ -z "${OPENROUTER_API_KEY:-}" ]]; then
  echo "Error: OPENROUTER_API_KEY is not set. Run ./set-keys.sh first." >&2
  exit 1
fi

cd "$ROOT"
export CHATBOT_DATA_DIR="$DATA_DIR"
export MODEL="${MODEL:-meta/muse-spark-1.2-contributor}"

detach() {
  # macOS has no setsid; nohup + disown keeps children alive after the launcher exits.
  nohup "$@" >>"$log_file" 2>&1 </dev/null &
  disown -h "$!" 2>/dev/null || true
}

echo "Starting voice backend on port $VOICE_PORT..." >&2
log_file="$STATE_DIR/server.log"
PORT="$VOICE_PORT" SERVER_LOG="$log_file" detach ./run-openrouter.sh

for _ in $(seq 1 180); do
  server_pid="$(listener "$VOICE_PORT")"
  [[ -n "$server_pid" ]] && break
  sleep 1
done
server_pid="$(listener "$VOICE_PORT")"
if [[ -z "$server_pid" ]]; then
  echo "Timed out waiting for voice port $VOICE_PORT." >&2
  tail -40 "$STATE_DIR/server.log" >&2 || true
  exit 1
fi

echo "Starting web app on port $WEB_PORT..." >&2
log_file="$STATE_DIR/web.log"
CHATBOT_VOICE_URL="ws://127.0.0.1:$VOICE_PORT/v1/realtime" \
  detach uv run uvicorn --app-dir web_app server:app --host 127.0.0.1 --port "$WEB_PORT"

for _ in $(seq 1 60); do
  web_pid="$(listener "$WEB_PORT")"
  if [[ -n "$web_pid" ]] && curl -sf "http://127.0.0.1:$WEB_PORT/api/config" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
web_pid="$(listener "$WEB_PORT")"
if [[ -z "$web_pid" ]] || ! curl -sf "http://127.0.0.1:$WEB_PORT/api/config" >/dev/null 2>&1; then
  echo "Timed out waiting for web port $WEB_PORT." >&2
  kill "$server_pid" 2>/dev/null || true
  [[ -n "$web_pid" ]] && kill "$web_pid" 2>/dev/null || true
  exit 1
fi

cat >"$STATE_DIR/state.env" <<EOF
RUN_ID=$RUN_ID
SERVER_PID=$server_pid
WEB_PID=$web_pid
WEB_PORT=$WEB_PORT
VOICE_PORT=$VOICE_PORT
DATA_DIR=$DATA_DIR
STATE_DIR=$STATE_DIR
ARTIFACTS_DIR=$ARTIFACTS_DIR
BASE_URL=http://127.0.0.1:$WEB_PORT
VOICE_WS_URL=ws://127.0.0.1:$VOICE_PORT/v1/realtime
EOF

curl -sf "http://127.0.0.1:$WEB_PORT/api/config" >"$ARTIFACTS_DIR/config.json"
echo "ready"
echo "state=$STATE_DIR/state.env"
echo "artifacts=$ARTIFACTS_DIR"
