# Voice orb

The voice orb starts a realtime conversation over the local WebSocket backend. It streams microphone audio out and plays synthesized speech back.

## Sub-features

- `orb-connect` opens the mic and connects to `VOICE_WS_URL`.
- `orb-listen` shows listening state after a successful session handshake.
- `orb-barge-in` allows speaking while the assistant talks (full duplex).
- `orb-stop` ends the session via the End control.

## How to get to it (user POV)

- Open `BASE_URL` and choose the orb labeled `Start voice conversation`.
- Grant microphone permission when the browser asks.
- Choose `End` to stop an active session.

## Driving it with browser-use

Preconditions:

- `doctor.sh` is green for this run.
- `OPENROUTER_API_KEY` is valid.
- Chrome can grant microphone access to `127.0.0.1` (manual approval may be required once per profile).
- Parakeet and TTS model weights are already downloaded, or the verifier accepts a long first-connection wait.

- **Start session.** Click `#main-circle` (accessible name `Start voice conversation`). Caption moves off `Tap to start` into `connecting`, then `listening` or an error state.
- **WebSocket path.** `GET /api/config` `chatbotUrl` must equal `VOICE_WS_URL` from `state.env`.
- **Stop session.** Click `#stop-btn` (accessible name `End`). Caption returns toward `Tap to start`.
- **Proof.** Save `orb-connected.png` showing a non-idle caption, plus `server.log` tail from the state directory. Optional: record a short mic utterance only when manual listening is acceptable; automated proof usually stops at a successful connect/listen state.

## Gotchas

- Headless automation often cannot pass mic capture. Treat connect/listen UI state as the automated bar; full STT→LLM→TTS proof may need a human listen.
- First model load after upgrade can exceed two minutes. Read `server.log` before blaming the UI.
- Echo control relies on browser AEC plus Silero VAD. Do not expect the mic to mute during assistant speech.
- Never run two voice backends on the same `VOICE_PORT`. Verification offsets ports for isolation.
