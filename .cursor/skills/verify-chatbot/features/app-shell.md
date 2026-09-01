# App shell

The app shell is the Chatbot browser page at `BASE_URL`. It loads static UI, fetches `/api/config`, and shows the idle orb with the caption `Tap to start`.

## Sub-features

- `shell-load` serves the main page and static assets.
- `shell-config` exposes the pinned voice WebSocket URL and tool availability.
- `shell-orb-idle` shows the start orb before a voice session connects.

## How to get to it (user POV)

- Open `http://127.0.0.1:7860` after `./run-browser.sh` (or the verification `BASE_URL` from `state.env`).

## Driving it with browser-use

Preconditions:

- `doctor.sh` reports all `ok` lines for this run's `state.env`.
- `BASE_URL` answers `GET /api/config`.

- **Load page.** Open `BASE_URL`. Run Browser-use `new_tab(BASE_URL)` then `wait_for_load()`. `page_info()` shows title `Chatbot`.
- **Idle orb.** Locate the button named `Start voice conversation`. Its `#circle-caption` neighbor has role `status` and text `Tap to start`.
- **Config endpoint.** Run `curl -sf "$BASE_URL/api/config"`. Response JSON includes `chatbotUrl` ending in `/v1/realtime` and boolean `search`, `codeAgent`, `desktopControl`.
- **Proof.** Save `config.json` (launcher already writes one) plus `browser_screenshot` to `artifacts/verify-chatbot/runs/$RUN_ID/app-shell/page.png`. Screenshot must show the Chatbot header and the idle orb caption.

## Gotchas

- First load after model download can take minutes before `/api/config` answers; wait on `doctor.sh`, not a fixed sleep.
- Verification runs use offset ports (`17860` / `18766` by default), not `7860` / `8766`.
- Static hosting without the FastAPI server leaves Settings editable but `/api/config` unreachable. Verification always launches the full stack.
