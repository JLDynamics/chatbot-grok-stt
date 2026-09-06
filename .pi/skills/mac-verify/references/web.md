# Web sidecar reference

- Launcher: `run-browser.sh` (owns `:8766` voice + `:7860` sidecar). Logs: `/tmp/chatbot-server.log`, `/tmp/chatbot-web.log`.
- App: `web_app/server.py:app` — FastAPI, `uvicorn --app-dir web_app server:app`.
- Bridge: `web_app/chrome_article_bridge/` (MV3, `BRIDGE_VERSION` in README).
- Key API: `http://127.0.0.1:7860/api/*` (`/api/health` or `/docs` for smoke).
- Tests: `tests/test_web_app_server.py` (largest suite, `TestClient` via importlib).
- Browser skill: `browser-tools` — `browser-nav.js`, `browser-eval.js`, `browser-screenshot.js` on Chrome `:9222`. Prefer `browser-eval.js` DOM queries over screenshots.
