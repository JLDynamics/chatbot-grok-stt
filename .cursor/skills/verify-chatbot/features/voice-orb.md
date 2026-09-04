# Voice backend

Voice conversations run in the native panel over the realtime WebSocket backend. Automated verification proves the backend accepts connections; a full spoken conversation needs the panel and a human listen.

## Sub-features

- `orb-connect` accepts a WebSocket client on `VOICE_WS_URL`.
- `orb-listen` completes a session handshake (native panel, manual).
- `orb-barge-in` allows speaking while the assistant talks (full duplex, manual).
- `orb-stop` ends the session from the panel (manual).

## How to get to it (user POV)

- Build and open the panel (`./macos/Voice/scripts/build.sh`, `open macos/Voice/build/Voice.app`).
- Click the orb and talk. Choose `End` to stop an active session.

## Driving it without the panel

Preconditions:

- `doctor.sh` is green for this run.
- `OPENROUTER_API_KEY` is valid.
- Parakeet and TTS model weights are already downloaded, or the verifier accepts a long first-connection wait.

- **Connect probe.** Run `.venv/bin/python -c` with `websockets.connect(VOICE_WS_URL)` and an empty session; a clean connect without handshake errors is the automated bar.
- **WebSocket path.** `GET /api/config` `chatbotUrl` must equal `VOICE_WS_URL` from `state.env`.
- **Proof.** Save the probe output plus `server.log` tail from the state directory.

## Gotchas

- Automated proof stops at a successful connect; full STT→LLM→TTS proof needs the native panel and may need a human listen.
- First model load after upgrade can exceed two minutes. Read `server.log` before blaming anything else.
- Echo control relies on the panel audio engine plus Silero VAD. Do not expect the mic to mute during assistant speech.
- Never run two voice backends on the same `VOICE_PORT`. Verification offsets ports for isolation.
