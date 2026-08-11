#!/usr/bin/env bash
#
# Run chatbot with OpenRouter as the LLM.
#
#   Ears  (STT):  Parakeet TDT   — local, MLX/MPS
#   Brain (LLM):  GPT-5.6 Luna   — remote, via OpenRouter
#   Mouth (TTS):  Qwen3-TTS      — local, MLX/MPS
#
# Usage:
#   export OPENROUTER_API_KEY=sk-or-v1-...
#   ./run-openrouter.sh              # server + mic/speaker client (default)
#   ./run-openrouter.sh serve        # server only, for external clients
#
# Override anything without editing this file:
#   MODEL=openai/gpt-5.6-luna-pro ./run-openrouter.sh
#   PROMPT="You are a sarcastic pirate." ./run-openrouter.sh

set -euo pipefail

# ── Config ────────────────────────────────────────────────────────────

COMMAND="${1:-local}"                                # local | serve
MODEL="${MODEL:-openai/gpt-5.6-luna}"
BASE_URL="${BASE_URL:-https://openrouter.ai/api/v1}"
# NOT 8765 (the upstream default): on this machine 8765 is permanently held by
# agent-note-mcp, a launchd-managed daemon that respawns as soon as it is killed.
PORT="${PORT:-8766}"
# NOTE: in browser mode (run-browser.sh) the demo sends its own instructions on
# every session, which override this. The matching browser default lives in
# demo/main.js (DEFAULT_INSTRUCTIONS); edit it there, or live in Settings.
PROMPT="${PROMPT:-You are a sharp, warm conversation partner talking out loud with Jack. Speak like a person, not an assistant: contractions, everyday words, short sentences. No lists, no markdown, no emoji. Everything you say is read aloud. Usually answer in one to three sentences. Go longer only when the topic truly needs it or Jack asks. React to what Jack actually said before adding your own thought. When it feels natural, end with one short question that moves things forward, but not every turn. Have opinions: if asked what you think, say it plainly and give your reason. Never say great question, never flatter, never pad with disclaimers. If you do not know something, say so in one sentence. Say numbers, dates, and units the way people speak them.}"

# Which physical mic/speaker to use, matched by NAME (indices shift whenever a
# USB or display device is plugged in, so names are the stable handle).
# "|" separates a preference list, tried left to right: use the USB mic and the
# monitor if they are plugged in, otherwise fall back to the laptop's built-ins.
# That way the same command works docked or undocked, with nothing to change.
#   Force the laptop:  MIC_NAME="MacBook Pro Microphone" SPK_NAME="MacBook Pro Speakers" ./run-openrouter.sh
MIC_NAME="${MIC_NAME:-ZTD39|MacBook Pro Microphone}"
SPK_NAME="${SPK_NAME:-SAMSUNG|MacBook Pro Speakers}"

# Mute the mic while the assistant is speaking. Required when output goes to
# SPEAKERS: the monitor plays the reply, the desk mic hears it, the app
# transcribes its own voice as user speech and interrupts itself mid-sentence.
# The cost is barge-in -- you cannot cut the assistant off by talking over it.
# On HEADPHONES there is no echo, so turn this off and get barge-in back:
#   HALF_DUPLEX=0 ./run-openrouter.sh
HALF_DUPLEX="${HALF_DUPLEX:-1}"

# Level-gated barge-in, so you CAN interrupt while still using speakers.
# While the assistant speaks, mic audio is forwarded only if its peak exceeds
# this (0.0-1.0). The echo bleeding back from the speakers sits below it; your
# closer, louder voice sits above it, so only you interrupt.
# 0 = off (mic fully muted during playback). Find your number with:
#   .venv/bin/python calibrate-barge-in.py
BARGE_IN_LEVEL="${BARGE_IN_LEVEL:-0}"

# TTS voice: one of the 9 Qwen3 CustomVoice presets.
#   deeper:  uncle_fu aiden ryan dylan
#   higher:  eric sohee vivian ono_anna serena
# Try one without editing this file:  VOICE=serena ./run-openrouter.sh
VOICE="${VOICE:-ryan}"

# How many sentences the TTS accumulates before generating. This is the single
# biggest lever on how NATURAL Qwen3 sounds. At 1, every sentence is its own
# generation with no prosody carried over, so pitch resets mid-reply -- measured
# on this machine, Ryan swung 186 -> 151 -> 138 Hz across three sentences. At 3
# (the upstream default) the same text held 128 -> 126 -> 120 Hz: a 6x smaller
# swing. The cost is latency -- it waits for more text before it starts talking.
#   Snappier but jumpier:  BATCH_SENTENCES=1 ./run-openrouter.sh
BATCH_SENTENCES="${BATCH_SENTENCES:-3}"

# Qwen3 model precision on Apple Silicon: bf16 | 8bit | 6bit | 4bit.
# Fewer bits = smaller and faster, but each number in the model is stored more
# coarsely, which shows up as a slightly grainier voice. bf16 is the original,
# uncompressed release; 8bit is near-indistinguishable from it for a third of
# the extra disk. Sizes: bf16 4.52 GB, 8bit 3.08 GB, 6bit 2.70 GB, 4bit 2.31 GB.
QUANT="${QUANT:-8bit}"

# Qwen3 sampling temperature. The model default (0.9) is expressive but each
# reply can land at a noticeably different pitch -- measured on this machine,
# six takes of one sentence spanned 87 Hz at 0.9 vs 37 Hz at 0.5. Lower is
# steadier, higher is livelier.
#   Livelier:  TTS_TEMP=0.9 ./run-openrouter.sh
TTS_TEMP="${TTS_TEMP:-0.5}"

# Qwen3 is the only TTS engine in this project (Kokoro and Pocket were removed).
TTS_ARGS=(--tts qwen3 --qwen3_tts_mlx_quantization "$QUANT" --qwen3_tts_speaker "$VOICE" --qwen3_tts_temperature "$TTS_TEMP")

# ── Preflight ─────────────────────────────────────────────────────────

if [[ -z "${OPENROUTER_API_KEY:-}" ]]; then
  cat >&2 <<'EOF'
Error: OPENROUTER_API_KEY is not set.

  1. Get a key at https://openrouter.ai/keys
  2. export OPENROUTER_API_KEY=sk-or-v1-...

To persist it, add that line to ~/.zshrc and open a new terminal.
EOF
  exit 1
fi

if [[ "$COMMAND" != "local" && "$COMMAND" != "serve" ]]; then
  echo "Error: expected 'local' or 'serve', got '$COMMAND'" >&2
  exit 1
fi

# Prefer the project's virtualenv if one exists, else whatever is on PATH.
if [[ -x ".venv/bin/chatbot" ]]; then
  S2S=".venv/bin/chatbot"
elif command -v chatbot >/dev/null 2>&1; then
  S2S="chatbot"
else
  echo "Error: chatbot not found. Install it with:  uv sync  (or  pip install -e .)" >&2
  exit 1
fi

# Fail fast if the port is taken, rather than after several minutes of model
# loading. Errno 48 at the very end of startup is otherwise very confusing.
# Only LISTENING sockets count. A plain `lsof -i :PORT` also matches outbound
# and already-CLOSED client connections, so a browser tab that once visited the
# port is enough to make it look occupied when nothing is serving there.
# The `|| true` matters: lsof exits non-zero when it finds nothing, and under
# `set -e` this assignment would then kill the script on a FREE port.
OCCUPANT_PID="$(lsof -ti "TCP:$PORT" -sTCP:LISTEN 2>/dev/null | head -1 || true)"
if [[ -n "$OCCUPANT_PID" ]]; then
  cat >&2 <<EOF
Error: port $PORT is already in use. The process holding it is:

$(ps -p "$OCCUPANT_PID" -o pid,ppid,etime,command 2>/dev/null)

If that is an earlier run of this script, switch to its Terminal window and
press Ctrl+C, or stop it with:  kill $OCCUPANT_PID

If it is something else entirely, do NOT kill it -- just use another port:

  PORT=8767 $0 $COMMAND

(Note: a process whose PPID is 1 is managed by launchd and will respawn
immediately after you kill it. Changing the port is the only fix there.)
EOF
  exit 1
fi

# ── Audio device selection (local mode only) ──────────────────────────
#
# sounddevice picks the macOS *system default* device when none is given. That
# default follows whatever was plugged in last, so the app can end up listening
# to a monitor's mic while you talk into the laptop. Resolve by name instead.

AUDIO_ARGS=()
MIC_LABEL="system default"
SPK_LABEL="system default"

if [[ "$COMMAND" == "local" ]]; then
  DEVICE_MAP="$("$(dirname "$S2S")/python" - "$MIC_NAME" "$SPK_NAME" <<'PY' 2>/dev/null || true
import sys
try:
    import sounddevice as sd
    devs = sd.query_devices()
except Exception:
    sys.exit(0)

def find(spec, kind):
    """Resolve the first device in a '|'-separated preference list that exists."""
    key = "max_input_channels" if kind == "in" else "max_output_channels"
    usable = [(i, d) for i, d in enumerate(devs) if d[key] > 0]
    for name in [n.strip() for n in spec.split("|") if n.strip()]:
        for i, d in usable:                  # exact name match first
            if d["name"] == name:
                return i, d["name"]
        for i, d in usable:                  # then a loose match
            if name.lower() in d["name"].lower():
                return i, d["name"]
    return None, None

for want, kind in ((sys.argv[1], "in"), (sys.argv[2], "out")):
    idx, label = find(want, kind)
    print(f"{idx if idx is not None else ''}\t{label or ''}")
PY
)"

  MIC_IDX="$(printf '%s\n' "$DEVICE_MAP" | sed -n '1p' | cut -f1)"
  MIC_FOUND="$(printf '%s\n' "$DEVICE_MAP" | sed -n '1p' | cut -f2)"
  SPK_IDX="$(printf '%s\n' "$DEVICE_MAP" | sed -n '2p' | cut -f1)"
  SPK_FOUND="$(printf '%s\n' "$DEVICE_MAP" | sed -n '2p' | cut -f2)"

  if [[ -n "${MIC_IDX:-}" ]]; then
    AUDIO_ARGS+=(--local_audio_input_device "$MIC_IDX")
    MIC_LABEL="[$MIC_IDX] $MIC_FOUND"
  else
    echo "Warning: no input device matching '$MIC_NAME' -- using the system default." >&2
  fi

  if [[ -n "${SPK_IDX:-}" ]]; then
    AUDIO_ARGS+=(--local_audio_output_device "$SPK_IDX")
    SPK_LABEL="[$SPK_IDX] $SPK_FOUND"
  else
    echo "Warning: no output device matching '$SPK_NAME' -- using the system default." >&2
  fi

  if [[ "$HALF_DUPLEX" != "0" ]]; then
    AUDIO_ARGS+=(--local_audio_block_mic_during_playback true)
    if [[ "$BARGE_IN_LEVEL" != "0" ]]; then
      AUDIO_ARGS+=(--local_audio_barge_in_level "$BARGE_IN_LEVEL")
      DUPLEX_LABEL="barge-in above level $BARGE_IN_LEVEL (speak up to interrupt)"
    else
      DUPLEX_LABEL="mic muted while assistant speaks (no barge-in)"
    fi
  else
    DUPLEX_LABEL="always listening (barge-in on -- use headphones)"
  fi
fi

echo "STT  parakeet-tdt   (local, MPS)"
echo "LLM  $MODEL   (OpenRouter)"
echo "TTS  qwen3          (local, MPS)  voice: $VOICE  precision: $QUANT"
echo "port $PORT"
if [[ "$COMMAND" == "local" ]]; then
  echo "mic  $MIC_LABEL"
  echo "out  $SPK_LABEL"
  echo "mode ${DUPLEX_LABEL:-}"
fi
echo

# ── Run ───────────────────────────────────────────────────────────────
#
# --mac-optimal-settings sets STT/TTS devices to MPS. Passing --llm_backend
# explicitly stops it from also forcing the local mlx-lm LLM, so the preset
# handles the audio stages while OpenRouter handles generation.

# The key is exported rather than passed as --responses_api_api_key, because
# command-line arguments are visible to every process on the machine via `ps`.
# With the flag omitted the OpenAI SDK reads OPENAI_API_KEY from the env
# (see LLM/base_openai_compatible_language_model.py, OpenAI(api_key=None,...)).
export OPENAI_API_KEY="$OPENROUTER_API_KEY"

exec "$S2S" "$COMMAND" \
  ${AUDIO_ARGS[@]+"${AUDIO_ARGS[@]}"} \
  --port "$PORT" \
  --mac-optimal-settings \
  --stt parakeet-tdt \
  "${TTS_ARGS[@]}" \
  --llm_backend responses-api \
  --model_name "$MODEL" \
  --responses_api_base_url "$BASE_URL" \
  --responses_api_stream \
  --no_responses_api_disable_thinking \
  --init_chat_prompt "$PROMPT" \
  --stream_batch_sentences "$BATCH_SENTENCES" \
  --enable_live_transcription
