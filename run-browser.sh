#!/usr/bin/env bash
#
# Run chatbot with the BROWSER client instead of the terminal one.
#
# Why: the browser gives you real acoustic echo cancellation for free --
# demo/main.js asks for getUserMedia({echoCancellation, noiseSuppression,
# autoGainControl}), the same WebRTC processing Google Meet and Zoom use. It
# subtracts the speaker output from the mic signal, so the assistant does not
# hear itself. That means natural barge-in ON SPEAKERS, with no headphones and
# no loudness threshold -- just talk over it.
#
# The terminal client (./run-openrouter.sh) has no echo cancellation, which is
# why it needs the mic muted or a BARGE_IN_LEVEL. This does not.
#
# Usage:
#   export OPENROUTER_API_KEY=sk-or-v1-...
#   ./run-browser.sh              # starts both halves, then open the URL
#
# Optional web search for the assistant (weather, news, prices). Either provider
# works -- the server picks one from the key's shape:
#   export TAVILY_API_KEY=tvly-...     # 1,000 searches/month, renews monthly
#   export SERPER_API_KEY=...          # 2,500 free credits, one-off
# You can also paste a key into Settings -> Tools in the browser instead.
#
# Everything run-openrouter.sh understands still applies, e.g.
#   VOICE=azelma ./run-browser.sh

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

# Load keys saved by ./set-keys.sh, so no export is needed in your shell.
# Values already in the environment win, so a one-off `KEY=... ./run-...` still
# overrides the stored file.
CHATBOT_ENV="$HOME/.config/chatbot/env"
if [[ -f "$CHATBOT_ENV" ]]; then
  _saved_openrouter="${OPENROUTER_API_KEY:-}"
  _saved_tavily="${TAVILY_API_KEY:-}"
  # shellcheck disable=SC1090
  source "$CHATBOT_ENV"
  [[ -n "$_saved_openrouter" ]] && OPENROUTER_API_KEY="$_saved_openrouter"
  [[ -n "$_saved_tavily" ]] && TAVILY_API_KEY="$_saved_tavily"
  export OPENROUTER_API_KEY TAVILY_API_KEY
fi

PORT="${PORT:-8766}"                 # chatbot server
WEB_PORT="${WEB_PORT:-7860}"         # browser UI
SERVER_LOG="${SERVER_LOG:-/tmp/s2s-server.log}"
WEB_LOG="${WEB_LOG:-/tmp/s2s-web.log}"

if [[ -z "${OPENROUTER_API_KEY:-}" ]]; then
  echo "Error: OPENROUTER_API_KEY is not set. See run-openrouter.sh." >&2
  exit 1
fi

# Only LISTENING sockets count as "the port is taken". A plain `lsof -i :PORT`
# also matches outbound and already-CLOSED client connections -- a browser tab
# that once visited the page is enough to make the port look occupied when
# nothing is actually serving on it.
# The `|| true` matters: lsof exits non-zero when it finds nothing, and under
# `set -e` a bare assignment from it would kill the script silently on a FREE
# port -- the exact opposite of what this check is for.
port_listener() { lsof -ti "TCP:$1" -sTCP:LISTEN 2>/dev/null | head -1 || true; }

for p in "$PORT" "$WEB_PORT"; do
  OCC="$(port_listener "$p")"
  if [[ -n "$OCC" ]]; then
    OCC_CMD="$(ps -p "$OCC" -o command= 2>/dev/null || true)"
    echo "Error: port $p is already in use (pid $OCC)." >&2
    if [[ "$OCC_CMD" == *chatbot* || "$OCC_CMD" == *"server:app"* ]]; then
      echo >&2
      echo "That is a leftover from an earlier run of this script. Stop it with:" >&2
      echo >&2
      echo "  kill $OCC" >&2
      echo >&2
      echo "then run this script again." >&2
    else
      echo "Stop it, or pick another with PORT=... / WEB_PORT=..." >&2
    fi
    exit 1
  fi
done

SERVER_PID=""
WEB_PID=""
stop_pid() {
  local pid="$1"
  [[ -z "$pid" ]] && return 0
  kill "$pid" 2>/dev/null || true
  # Give it a moment, then insist. A server still loading models can ignore the
  # TERM and keep holding the port.
  for _ in $(seq 1 20); do
    kill -0 "$pid" 2>/dev/null || return 0
    sleep 0.25
  done
  kill -9 "$pid" 2>/dev/null || true
}
cleanup() {
  stop_pid "$WEB_PID"
  stop_pid "$SERVER_PID"
}
trap cleanup EXIT INT TERM

echo "Starting the chatbot server (models load first, ~10s)..."
echo "  log: $SERVER_LOG"
PORT="$PORT" ./run-openrouter.sh serve >"$SERVER_LOG" 2>&1 &
SERVER_PID=$!

# Wait for the websocket server to bind, surfacing failures instead of hanging.
for _ in $(seq 1 120); do
  if [[ -n "$(port_listener "$PORT")" ]]; then break; fi
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    echo "Server exited before it could start. Last lines of $SERVER_LOG:" >&2
    tail -20 "$SERVER_LOG" >&2
    exit 1
  fi
  sleep 1
done

if [[ -z "$(port_listener "$PORT")" ]]; then
  echo "Server did not bind port $PORT in time. See $SERVER_LOG." >&2
  exit 1
fi

echo "Server is up on ws://127.0.0.1:$PORT/v1/realtime"
echo
echo "  Open  ->  http://localhost:$WEB_PORT"
echo "  Click the orb, allow the microphone, then just talk."
echo "  You CAN interrupt it -- the browser cancels the echo for you."
echo
echo "Ctrl+C stops both halves."
echo

# Two things matter here, both learned the hard way:
#
#  1. NOT `exec` -- exec replaces this shell with uvicorn, taking the EXIT trap
#     with it and orphaning the server started above. It gets reparented to
#     launchd and holds the port forever, so the next run fails to start.
#  2. Background + `wait`, not a foreground child -- bash defers traps until a
#     foreground command finishes, so a `kill` from another terminal would not
#     clean up until uvicorn happened to exit. `wait` is interruptible, so the
#     trap runs immediately and both halves stop together.
# Output is teed, not just printed: web-search failures are diagnosed from this
# log, and "it's on my terminal" is not something that can be read back later.
SPEECH_TO_SPEECH_URL="ws://localhost:$PORT/v1/realtime" \
STARTUP_GREETING="${STARTUP_GREETING:-}" \
uv run --with-requirements demo/requirements.txt \
    uvicorn --app-dir demo server:app --port "$WEB_PORT" 2>&1 | tee "$WEB_LOG" &
WEB_PID=$!
wait "$WEB_PID"
