#!/usr/bin/env bash
# Start the single supported realtime backend:
# Parakeet MLX -> Responses API -> Sesame CSM-1B MLX.
set -euo pipefail

CHATBOT_ENV="$HOME/.config/chatbot/env"
if [[ -f "$CHATBOT_ENV" ]]; then
  saved_openrouter="${OPENROUTER_API_KEY:-}"
  # shellcheck disable=SC1090
  source "$CHATBOT_ENV"
  [[ -n "$saved_openrouter" ]] && OPENROUTER_API_KEY="$saved_openrouter"
fi

MODEL="${MODEL:-openai/gpt-5.6-luna}"
PORT="${PORT:-8766}"
CSM_MODEL="${CSM_MODEL:-mlx-community/csm-1b-8bit}"
CSM_VOICE="${CSM_VOICE:-conversational_b}"
CSM_TEMP="${CSM_TEMP:-0.55}"
BATCH_SENTENCES="${CSM_BATCH_SENTENCES:-${BATCH_SENTENCES:-3}}"
PROMPT="${PROMPT:-You are a concise, friendly voice assistant. Speak naturally without markdown or lists.}"
VAD_THRESH="${VAD_THRESH:-0.55}"
VAD_MIN_SILENCE_MS="${VAD_MIN_SILENCE_MS:-350}"
VAD_MIN_SPEECH_MS="${VAD_MIN_SPEECH_MS:-400}"
VAD_SPEECH_PAD_MS="${VAD_SPEECH_PAD_MS:-500}"
VAD_SHORT_SEGMENT_MERGE_MS="${VAD_SHORT_SEGMENT_MERGE_MS:-400}"
PARAKEET_LANG="${PARAKEET_LANG:-en}"

if [[ -z "${OPENROUTER_API_KEY:-}" ]]; then
  echo "Error: OPENROUTER_API_KEY is not set. Run ./set-keys.sh first." >&2
  exit 1
fi

if [[ -x ".venv/bin/chatbot" ]]; then
  CHATBOT_BIN=".venv/bin/chatbot"
elif command -v chatbot >/dev/null 2>&1; then
  CHATBOT_BIN="chatbot"
else
  echo "Error: chatbot is not installed. Run: uv sync" >&2
  exit 1
fi

occupant="$(lsof -ti "TCP:$PORT" -sTCP:LISTEN 2>/dev/null | head -1 || true)"
if [[ -n "$occupant" ]]; then
  echo "Error: port $PORT is already in use by pid $occupant." >&2
  exit 1
fi

exec "$CHATBOT_BIN" serve \
  --host 127.0.0.1 \
  --port "$PORT" \
  --stt parakeet-tdt \
  --llm_backend responses-api \
  --tts csm \
  --csm_tts_model_name "$CSM_MODEL" \
  --csm_tts_voice "$CSM_VOICE" \
  --csm_tts_temperature "$CSM_TEMP" \
  --model_name "$MODEL" \
  --responses_api_stream \
  --no_responses_api_disable_thinking \
  --init_chat_prompt "$PROMPT" \
  --stream_batch_sentences "$BATCH_SENTENCES" \
  --thresh "$VAD_THRESH" \
  --min_silence_ms "$VAD_MIN_SILENCE_MS" \
  --min_speech_ms "$VAD_MIN_SPEECH_MS" \
  --speech_pad_ms "$VAD_SPEECH_PAD_MS" \
  --short_segment_merge_ms "$VAD_SHORT_SEGMENT_MERGE_MS" \
  --parakeet_tdt_language "$PARAKEET_LANG" \
  --enable_live_transcription
