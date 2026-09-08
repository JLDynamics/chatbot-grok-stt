#!/usr/bin/env bash
# Start the single supported realtime backend:
# Parakeet MLX -> Responses API -> VibeVoice (Microsoft) TTS.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Prefer this checkout's sources so a shared .venv (git worktree) still runs
# the code you just edited.
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"

CHATBOT_ENV="$HOME/.config/chatbot/env"
if [[ -f "$CHATBOT_ENV" ]]; then
  saved_openrouter="${OPENROUTER_API_KEY:-}"
  # shellcheck disable=SC1090
  source "$CHATBOT_ENV"
  [[ -n "$saved_openrouter" ]] && OPENROUTER_API_KEY="$saved_openrouter"
fi

MODEL="${MODEL:-openai/gpt-5.6-luna}"
PORT="${PORT:-8766}"
BATCH_SENTENCES="${BATCH_SENTENCES:-3}"
CHAT_SIZE="${CHAT_SIZE:-20}"
PROMPT="${PROMPT:-You are a concise, friendly voice assistant. Speak naturally without markdown or lists.}"
VAD_THRESH="${VAD_THRESH:-0.60}"
VAD_MIN_SILENCE_MS="${VAD_MIN_SILENCE_MS:-1200}"
VAD_MIN_SPEECH_MS="${VAD_MIN_SPEECH_MS:-600}"
VAD_SPEECH_PAD_MS="${VAD_SPEECH_PAD_MS:-500}"
VAD_SHORT_SEGMENT_MERGE_MS="${VAD_SHORT_SEGMENT_MERGE_MS:-400}"
PARAKEET_MODEL="${PARAKEET_MODEL:-mlx-community/parakeet-tdt-1.1b}"
PARAKEET_LANG="${PARAKEET_LANG:-en}"

if [[ -z "${OPENROUTER_API_KEY:-}" ]]; then
  echo "Error: OPENROUTER_API_KEY is not set. Run ./set-keys.sh first." >&2
  exit 1
fi

if [[ -x "$ROOT/.venv/bin/chatbot" ]]; then
  CHATBOT_BIN="$ROOT/.venv/bin/chatbot"
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

TTS="${TTS:-kokoro}"

# Kokoro (default)
KOKORO_MODEL="${KOKORO_MODEL:-mlx-community/Kokoro-82M-bf16}"
KOKORO_VOICE="${KOKORO_VOICE:-bm_fable}"
KOKORO_LANG="${KOKORO_LANG:-b}"
KOKORO_SPEED="${KOKORO_SPEED:-1.0}"
KOKORO_BLOCKSIZE="${KOKORO_BLOCKSIZE:-512}"
KOKORO_DENOISE_FLOOR="${KOKORO_DENOISE_FLOOR:-0.04}"

# VibeVoice (alternative)
VIBEVOICE_MODEL="${VIBEVOICE_MODEL:-mlx-community/VibeVoice-Realtime-0.5B-8bit}"
VIBEVOICE_VOICE="${VIBEVOICE_VOICE:-en-Emma_woman}"
VIBEVOICE_MAX_TOKENS="${VIBEVOICE_MAX_TOKENS:-1024}"
VIBEVOICE_CFG_SCALE="${VIBEVOICE_CFG_SCALE:-1.5}"
VIBEVOICE_DENOISE_FLOOR="${VIBEVOICE_DENOISE_FLOOR:-0.04}"

args=(
  serve
  --host 127.0.0.1
  --port "$PORT"
  --stt parakeet-tdt
  --llm_backend responses-api
  --tts "$TTS"
)

if [[ "$TTS" == "kokoro" ]]; then
  args+=(
    --kokoro_tts_model_name "$KOKORO_MODEL"
    --kokoro_tts_voice "$KOKORO_VOICE"
    --kokoro_tts_lang_code "$KOKORO_LANG"
    --kokoro_tts_speed "$KOKORO_SPEED"
    --kokoro_tts_blocksize "$KOKORO_BLOCKSIZE"
    --kokoro_tts_gen_spectral_denoise_floor "$KOKORO_DENOISE_FLOOR"
  )
else
  args+=(
    --vibevoice_tts_model_name "$VIBEVOICE_MODEL"
    --vibevoice_tts_voice "$VIBEVOICE_VOICE"
    --vibevoice_tts_max_tokens "$VIBEVOICE_MAX_TOKENS"
    --vibevoice_tts_cfg_scale "$VIBEVOICE_CFG_SCALE"
    --vibevoice_tts_gen_spectral_denoise_floor "$VIBEVOICE_DENOISE_FLOOR"
  )
fi

args+=(
  --model_name "$MODEL"
  --responses_api_stream
  --no_responses_api_disable_thinking
  --init_chat_prompt "$PROMPT"
  --stream_batch_sentences "$BATCH_SENTENCES"
  --chat_size "$CHAT_SIZE"
  --thresh "$VAD_THRESH"
  --min_silence_ms "$VAD_MIN_SILENCE_MS"
  --min_speech_ms "$VAD_MIN_SPEECH_MS"
  --speech_pad_ms "$VAD_SPEECH_PAD_MS"
  --short_segment_merge_ms "$VAD_SHORT_SEGMENT_MERGE_MS"
  --parakeet_tdt_model_name "$PARAKEET_MODEL"
  --parakeet_tdt_language "$PARAKEET_LANG"
  --enable_live_transcription
)

exec "$CHATBOT_BIN" "${args[@]}"
