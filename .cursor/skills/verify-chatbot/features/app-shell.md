# Sidecar health

The sidecar at `BASE_URL` serves the native app's `/api/*`. There is no browser UI; `GET /` answers a stub.

## Sub-features

- `shell-root` answers the removal stub.
- `shell-config` exposes the voice WebSocket URL and tool availability.
- `shell-sessions` lists saved conversations.

## Checks

Preconditions:

- `doctor.sh` reports all `ok` lines for this run's `state.env`.

- **Root stub.** Run `curl -sf "$BASE_URL/"`. Response JSON is `{"ok":true,"service":"chatbot-sidecar","ui":"removed"}`.
- **Static gone.** `curl -s -o /dev/null -w '%{http_code}' "$BASE_URL/main.js"` prints `404`.
- **Config endpoint.** Run `curl -sf "$BASE_URL/api/config"`. Response JSON includes `chatbotUrl` ending in `/v1/realtime` and boolean `search`, `codeAgent`, `desktopControl`.
- **Sessions smoke.** Run `curl -sf "$BASE_URL/api/sessions"`. Response JSON has a `sessions` array.
- **Proof.** Save `config.json` (launcher already writes one) to `artifacts/verify-chatbot/runs/$RUN_ID/app-shell/`.

## Gotchas

- First load after model download can take minutes before `/api/config` answers; wait on `doctor.sh`, not a fixed sleep.
- Verification runs use offset ports (`17860` / `18766` by default), not `7860` / `8766`.
- Verification always launches the full stack (backend plus sidecar).
