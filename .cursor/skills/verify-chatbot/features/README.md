# Chatbot verification map

This directory is the maintained source for verifying user-facing Chatbot behavior. Read this index before driving the app, then open the matching feature file.

## Baseline preconditions

- Launch with `.cursor/skills/verify-chatbot/scripts/launch.sh` from the repo root.
- Use the disposable `CHATBOT_DATA_DIR` and offset ports recorded in `/tmp/chatbot-verify-<run-id>/state.env`.
- Run `.cursor/skills/verify-chatbot/scripts/doctor.sh` on that state file before driving.
- Never drive the user's default instance on ports `7860` / `8766` unless this run started it.

## Driving conventions

- Start every recipe from the baseline state unless its preconditions say otherwise.
- Prefer ARIA roles and accessible names from `web_app/index.html` over CSS selectors.
- Browser actions go through Browser-use MCP (`browser_exec`, `browser_screenshot`).
- HTTP actions use `curl` against `BASE_URL` from `state.env`.
- Restore or delete scratch data created during a mutation recipe. Do not delete proof artifacts during cleanup.

## Proof and skip reporting

- Capture the user action and the resulting state, not only the final screen.
- UI proof includes a screenshot and, when practical, an accessibility snapshot.
- API proof includes the command, response JSON, and a read-back.
- File-backed proof includes a second read of the on-disk path under `DATA_DIR`.
- Record the feature ID and entry point with every artifact.
- Report an unreachable path with the attempted command and the unmet precondition.
- Do not report a skipped entry point as verified through a different path.

## Feature entry contract

Each feature file starts with an H1 title and one paragraph describing the user-visible behavior. It then uses exactly four H2 sections in this order.

1. `Sub-features` lists short IDs with one line for each behavior.
2. `How to get to it (user POV)` lists every user entry point.
3. `Driving it with browser-use` starts with `Preconditions:` and uses labeled bullets that pair each user action with an exact command and observable result.
4. `Gotchas` lists traps that can waste or invalidate a verification run.

## Features

- [App shell](./app-shell.md) covers initial load, config discovery, and the idle orb.
- [Personal memory](./personal-memory.md) covers the Tools profile editor and server persistence.
- [Saved sessions](./saved-sessions.md) covers creating, listing, and deleting saved conversations.
- [Web search](./web-search.md) covers the Tools search toggle and `/api/search`.
- [Voice orb](./voice-orb.md) covers connecting to the realtime voice backend (mic required).
