#!/usr/bin/env bash
# Start the local realtime backend and the API sidecar for the native macOS app.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

CHATBOT_ENV="${CHATBOT_ENV:-$HOME/.config/chatbot/env}"
if [[ -f "$CHATBOT_ENV" ]]; then
  saved_openrouter="${OPENROUTER_API_KEY:-}"
  saved_tavily="${TAVILY_API_KEY:-}"
  saved_serper="${SERPER_API_KEY:-}"
  saved_tinyfish="${TINYFISH_API_KEY:-}"
  # shellcheck disable=SC1090
  source "$CHATBOT_ENV"
  [[ -n "$saved_openrouter" ]] && OPENROUTER_API_KEY="$saved_openrouter"
  [[ -n "$saved_tavily" ]] && TAVILY_API_KEY="$saved_tavily"
  [[ -n "$saved_serper" ]] && SERPER_API_KEY="$saved_serper"
  [[ -n "$saved_tinyfish" ]] && TINYFISH_API_KEY="$saved_tinyfish"
  export OPENROUTER_API_KEY TAVILY_API_KEY SERPER_API_KEY TINYFISH_API_KEY
fi

PORT="${PORT:-8766}"
WEB_PORT="${WEB_PORT:-7860}"
SERVER_LOG="${SERVER_LOG:-/tmp/chatbot-server.log}"
WEB_LOG="${WEB_LOG:-/tmp/chatbot-web.log}"
MODEL="${MODEL:-openai/gpt-5.6-luna}"
export MODEL

listener() { lsof -ti "TCP:$1" -sTCP:LISTEN 2>/dev/null | head -1 || true; }
reuse_running=false
sidecar_only=false
for arg in "$@"; do
  case "$arg" in
    --reuse-running) reuse_running=true ;;
    --sidecar-only) sidecar_only=true ;;
    *)
      echo "Unknown argument: $arg" >&2
      exit 1
      ;;
  esac
done
ports=("$WEB_PORT")
if [[ "$sidecar_only" == false ]]; then
  ports=("$PORT" "$WEB_PORT")
fi
for port in "${ports[@]}"; do
  pid="$(listener "$port")"
  if [[ -n "$pid" && "$reuse_running" == false ]]; then
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
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

if [[ "$sidecar_only" == false ]]; then
  if [[ -z "$(listener "$PORT")" ]]; then
    echo "Starting the Chatbot voice service..."
    PORT="$PORT" ./run-openrouter.sh >"$SERVER_LOG" 2>&1 &
    server_pid=$!
  fi
  for _ in $(seq 1 120); do
    [[ -n "$(listener "$PORT")" ]] && break
    if [[ -n "$server_pid" ]] && ! kill -0 "$server_pid" 2>/dev/null; then
      tail -20 "$SERVER_LOG" >&2
      exit 1
    fi
    sleep 1
  done
  if [[ -z "$(listener "$PORT")" ]]; then
    echo "The model server did not start. See $SERVER_LOG." >&2
    exit 1
  fi
  echo "Sidecar on http://localhost:$WEB_PORT (API only, no browser UI). Voice backend on port $PORT."
else
  echo "Sidecar on http://localhost:$WEB_PORT (API only, no browser UI)."
fi
echo "Ctrl+C stops the services started by this launcher."
if [[ -z "$(listener "$WEB_PORT")" ]]; then
  if [[ -x .venv/bin/uvicorn ]]; then
    sidecar_command=(.venv/bin/uvicorn)
  else
    sidecar_command=(uv run uvicorn)
  fi
  STARTUP_GREETING="${STARTUP_GREETING:-}" WEB_PORT="$WEB_PORT" \
    "${sidecar_command[@]}" --app-dir web_app server:app --host 127.0.0.1 --port "$WEB_PORT" \
    >"$WEB_LOG" 2>&1 &
  web_pid=$!
fi
# Supervise only children this launcher owns. An existing external service
# must not be killed when this launcher exits or its other child fails.
while [[ -n "$server_pid" || -n "$web_pid" ]]; do
  for child in "$server_pid" "$web_pid"; do
    if [[ -n "$child" ]] && ! kill -0 "$child" 2>/dev/null; then
      echo "A Chatbot service stopped. Check $SERVER_LOG and $WEB_LOG." >&2
      exit 1
    fi
  done
  sleep 1
done
