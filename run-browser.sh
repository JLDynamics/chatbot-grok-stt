#!/usr/bin/env bash
# Start the local realtime backend and browser product together.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

CHATBOT_ENV="$HOME/.config/chatbot/env"
if [[ -f "$CHATBOT_ENV" ]]; then
  saved_openrouter="${OPENROUTER_API_KEY:-}"
  saved_tavily="${TAVILY_API_KEY:-}"
  saved_serper="${SERPER_API_KEY:-}"
  # shellcheck disable=SC1090
  source "$CHATBOT_ENV"
  [[ -n "$saved_openrouter" ]] && OPENROUTER_API_KEY="$saved_openrouter"
  [[ -n "$saved_tavily" ]] && TAVILY_API_KEY="$saved_tavily"
  [[ -n "$saved_serper" ]] && SERPER_API_KEY="$saved_serper"
  export OPENROUTER_API_KEY TAVILY_API_KEY SERPER_API_KEY
fi

PORT="${PORT:-8766}"
WEB_PORT="${WEB_PORT:-7860}"
SERVER_LOG="${SERVER_LOG:-/tmp/chatbot-server.log}"
WEB_LOG="${WEB_LOG:-/tmp/chatbot-web.log}"
MODEL="${MODEL:-openai/gpt-5.6-luna}"
export MODEL

listener() { lsof -ti "TCP:$1" -sTCP:LISTEN 2>/dev/null | head -1 || true; }
for port in "$PORT" "$WEB_PORT"; do
  pid="$(listener "$port")"
  if [[ -n "$pid" ]]; then
    echo "Error: port $port is already in use by pid $pid." >&2
    exit 1
  fi
done

server_pid=""
web_pid=""
cleanup() {
  [[ -n "$web_pid" ]] && kill "$web_pid" 2>/dev/null || true
  [[ -n "$server_pid" ]] && kill "$server_pid" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

echo "Starting the voice models..."
PORT="$PORT" ./run-openrouter.sh >"$SERVER_LOG" 2>&1 &
server_pid=$!
for _ in $(seq 1 120); do
  [[ -n "$(listener "$PORT")" ]] && break
  if ! kill -0 "$server_pid" 2>/dev/null; then
    tail -20 "$SERVER_LOG" >&2
    exit 1
  fi
  sleep 1
done
if [[ -z "$(listener "$PORT")" ]]; then
  echo "The model server did not start. See $SERVER_LOG." >&2
  exit 1
fi

echo "Open http://localhost:$WEB_PORT and click the orb."
echo "Ctrl+C stops both processes."
SPEECH_TO_SPEECH_URL="ws://localhost:$PORT/v1/realtime" \
  STARTUP_GREETING="${STARTUP_GREETING:-}" \
  uv run uvicorn --app-dir web_app server:app --host 127.0.0.1 --port "$WEB_PORT" \
  2>&1 | tee "$WEB_LOG" &
web_pid=$!
wait "$web_pid"
