#!/usr/bin/env bash
# Read-only health check for a verification instance.
set -euo pipefail

STATE_FILE="${1:-${VERIFY_STATE_FILE:-}}"
if [[ -z "$STATE_FILE" || ! -f "$STATE_FILE" ]]; then
  echo "Usage: doctor.sh /tmp/chatbot-verify-<run-id>/state.env" >&2
  exit 2
fi

# shellcheck disable=SC1090
source "$STATE_FILE"

fail=0
report() {
  local label="$1"
  local ok="$2"
  if [[ "$ok" == "1" ]]; then
    echo "ok $label"
  else
    echo "fail $label"
    fail=1
  fi
}

if [[ -n "${SERVER_PID:-}" ]] && kill -0 "$SERVER_PID" 2>/dev/null; then report server_alive 1; else report server_alive 0; fi
if [[ -n "${WEB_PID:-}" ]] && kill -0 "$WEB_PID" 2>/dev/null; then report web_alive 1; else report web_alive 0; fi
if lsof -ti "TCP:$WEB_PORT" -sTCP:LISTEN >/dev/null 2>&1; then report web_port_listening 1; else report web_port_listening 0; fi
if lsof -ti "TCP:$VOICE_PORT" -sTCP:LISTEN >/dev/null 2>&1; then report voice_port_listening 1; else report voice_port_listening 0; fi

if config_json="$(curl -sf "$BASE_URL/api/config")"; then
  report config_fetch 1
  if echo "$config_json" | python3 -c 'import json,sys; d=json.load(sys.stdin); assert "chatbotUrl" in d and d["chatbotUrl"].endswith("/v1/realtime")'; then
    report config_shape 1
  else
    report config_shape 0
  fi
  expected_ws="ws://127.0.0.1:$VOICE_PORT/v1/realtime"
  actual_ws="$(echo "$config_json" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("chatbotUrl",""))')"
  if [[ "$actual_ws" == "$expected_ws" ]]; then
    report voice_url 1
  else
    echo "fail voice_url expected=$expected_ws actual=$actual_ws"
    fail=1
  fi
else
  report config_fetch 0
  report config_shape 0
  report voice_url 0
fi

if [[ -d "$DATA_DIR" ]]; then report data_dir 1; else report data_dir 0; fi

exit "$fail"
