#!/usr/bin/env bash
# Start the single supported realtime backend:
# Parakeet MLX -> Responses API -> Qwen3 CustomVoice MLX.
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
VOICE="${VOICE:-Ryan}"
QUANT="${QUANT:-8bit}"
TTS_TEMP="${TTS_TEMP:-0.5}"
BATCH_SENTENCES="${BATCH_SENTENCES:-3}"
PROMPT="${PROMPT:-You are a concise, friendly voice assistant. Speak naturally without markdown or lists.}"

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
  --tts qwen3 \
  --model_name "$MODEL" \
  --responses_api_stream \
  --no_responses_api_disable_thinking \
  --init_chat_prompt "$PROMPT" \
  --stream_batch_sentences "$BATCH_SENTENCES" \
  --qwen3_tts_speaker "$VOICE" \
  --qwen3_tts_mlx_quantization "$QUANT" \
  --qwen3_tts_temperature "$TTS_TEMP" \
  --enable_live_transcription
