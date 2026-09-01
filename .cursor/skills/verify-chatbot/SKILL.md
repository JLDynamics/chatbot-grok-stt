---
name: verify-chatbot
description: >-
  Launch, drive, and capture proof for the Chatbot Mac voice web app at
  http://127.0.0.1:7860. Use when verifying Chatbot behavior end to end,
  reproducing a UI or API bug, or proving a feature works before shipping.
disable-model-invocation: true
---

# Verify Chatbot

Drive the real Chatbot product the way a user does. The primary surface is the browser UI (`web_app/`). The voice backend (`chatbot serve` on port 8766) must be up for orb conversations; many features also expose HTTP routes on the same web origin.

Never drive the user's everyday instance on ports `7860` / `8766` unless this run started it. Verification uses disposable ports and a disposable data directory.

## Launch

From the repo root:

```bash
chmod +x .cursor/skills/verify-chatbot/scripts/*.sh
.cursor/skills/verify-chatbot/scripts/launch.sh
```

The launcher:

- Picks `VERIFY_RUN_ID` (or generates one) and writes state to `/tmp/chatbot-verify-$RUN_ID/state.env`
- Uses `VERIFY_WEB_PORT` (default `17860`) and `VERIFY_VOICE_PORT` (default `18766`)
- Sets `CHATBOT_DATA_DIR=/tmp/chatbot-verify-$RUN_ID/data` so sessions and personal memory do not touch `~/.chatbot`
- Reads `OPENROUTER_API_KEY` from `~/.config/chatbot/env` (run `./set-keys.sh` if missing)
- Starts the voice backend and web app with `nohup` and `disown` so both survive after the launcher exits (macOS has no `setsid`)

Ready means the script prints `ready` and paths for `state=` and `artifacts=`.

Proof artifacts for the run live under `artifacts/verify-chatbot/runs/$RUN_ID/`. Process logs live under `/tmp/chatbot-verify-$RUN_ID/`.

## Doctor

Run before driving whenever anything looks off:

```bash
.cursor/skills/verify-chatbot/scripts/doctor.sh /tmp/chatbot-verify-<run-id>/state.env
```

Require every line to start with `ok`. A `fail` line means do not drive; read `server.log` and `web.log` in the state directory, then relaunch or clean up.

## Drive

Read [features/README.md](features/README.md) first. Each feature file is the maintained recipe for one user-facing behavior.

**Harness.** Browser interactions use the Browser-use MCP (`browser_exec`, `browser_screenshot`). API checks use `curl` against `BASE_URL` from `state.env`. Prefer ARIA roles and accessible names from `web_app/index.html` over CSS classes.

**Browser recipe skeleton.**

```bash
browser-use <<'PY'
new_tab("http://127.0.0.1:17860")  # use BASE_URL from state.env
wait_for_load()
print(page_info())
PY
```

After navigation, locate controls with the accessibility tree (`cdp("Accessibility.getFullAXTree")`) or stable ids such as `#tools-btn`, `#personal-memory-editor`, `#main-circle`.

**Voice conversations** need a real microphone grant in Chrome. For deterministic CI-style proof, prefer HTTP-mapped features (personal memory, saved sessions, config) unless the task explicitly requires audio.

Mapped features and entry points:

| Feature | Browser entry | API entry |
| --- | --- | --- |
| App shell | Open `BASE_URL` | `GET /api/config` |
| Personal memory | Tools → Personal memory | `GET/PUT /api/personal-memory` |
| Saved sessions | Saved conversations | `GET/POST/PATCH/DELETE /api/sessions` |
| Web search | Tools → Local web search | `POST /api/search` (needs Tavily or Serper key) |
| Voice orb | Tap `Start voice conversation` | WebSocket `VOICE_WS_URL` |

## Evidence

Capture both the action and the resulting state.

- **UI proof:** `browser_screenshot` plus an accessibility snapshot saved under `artifacts/verify-chatbot/runs/$RUN_ID/<feature>/`
- **API proof:** request command, response JSON, and a read-back (`curl` after `PUT`)
- **File proof:** when a feature writes disk state, read the file under `CHATBOT_DATA_DIR` named in `state.env`

Standards:

- Exercise the user path, not test-only hooks
- Record `feature`, `entry point`, and `run_id` in a `proof.txt` beside artifacts
- A modal opening alone is not proof; confirm persistence with a second read
- Report unreachable paths with the failing command and the unmet precondition. Do not claim a different entry point verified the feature

Helper for the personal-memory API path:

```bash
.cursor/skills/verify-chatbot/scripts/prove-personal-memory.sh /tmp/chatbot-verify-<run-id>/state.env
```

## Cleanup

```bash
.cursor/skills/verify-chatbot/scripts/cleanup.sh /tmp/chatbot-verify-<run-id>/state.env
```

Cleanup kills only the `SERVER_PID` and `WEB_PID` recorded in `state.env`, then removes the disposable data directory. It never deletes `artifacts/verify-chatbot/runs/$RUN_ID/`. After cleanup, confirm proof files still exist.

Set `VERIFY_KEEP_DATA=1` to retain the data directory for debugging.

## Helpers

All scripts live in `.cursor/skills/verify-chatbot/scripts/` and take the `state.env` path as their first argument when noted.

| Script | Purpose |
| --- | --- |
| `launch.sh` | Start isolated instance, write `state.env`, wait for readiness |
| `doctor.sh` | Read-only health check |
| `prove-personal-memory.sh` | API proof for personal memory |
| `cleanup.sh` | Stop processes, remove scratch data, keep artifacts |

## Gotchas

- Apple Silicon Mac required. First launch downloads Parakeet and TTS weights; allow several minutes.
- `run-browser.sh` refuses to start if the chosen ports are already taken. Use custom `VERIFY_WEB_PORT` / `VERIFY_VOICE_PORT` or clean up the prior run.
- Personal memory persists in `$DATA_DIR/personal-memory.md`, not in the browser profile.
- Web search proof needs `TAVILY_API_KEY` or `SERPER_API_KEY` in `~/.config/chatbot/env`.
- Voice orb proof needs mic permission and a working OpenRouter model. Timeouts here are often model load, not UI bugs.
- Do not kill by process name (`chatbot`, `uvicorn`). Always use `cleanup.sh` with the run's `state.env`.

## Maintenance

After app changes, run `/maintain-verification-skill` to refresh selectors and feature recipes.
