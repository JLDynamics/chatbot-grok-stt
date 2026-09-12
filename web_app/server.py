"""Local API sidecar for the native macOS app: search, memory, sessions, code, and desktop tools.

Thin assembly over the per-concern ``web_app`` modules: this file builds the
FastAPI app, includes one router per concern, and serves the two shared
endpoints (``/api/config`` and ``/``). All behaviour lives in:

- :mod:`web_app.common` — env config, pooled HTTP client, URL/gating helpers
- :mod:`web_app.browser` — Chrome page-bridge store and ``/api/browser/*``
- :mod:`web_app.search` — ``/api/search``
- :mod:`web_app.fetch` — ``/api/fetch`` (the single fetch implementation)
- :mod:`web_app.read_page` — ``/api/read_page`` ladder (reuses fetch + bridge)
- :mod:`web_app.desktop` — ``/api/desktop/act``
- :mod:`web_app.sessions` — ``/api/sessions/*``, history search, personal memory
"""

from __future__ import annotations

import sys
from pathlib import Path

# Launched as ``uvicorn --app-dir web_app server:app``, where ``web_app`` is
# on sys.path but its parent is not. Make the ``web_app.*`` package imports
# below resolve in that layout too (a no-op under pytest, where repo root is
# already importable).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI  # noqa: E402
from web_app import browser, common, desktop, fetch, read_page, search, sessions  # noqa: E402

app = FastAPI(lifespan=common._lifespan)

app.include_router(browser.router)
app.include_router(search.router)
app.include_router(fetch.router)
app.include_router(read_page.router)
app.include_router(desktop.router)
app.include_router(sessions.router)


@app.get("/api/config")
def config() -> dict:
    return {
        "search": bool(common.TINYFISH_KEY or common.SERPER_KEY or common.TAVILY_KEY),
        "allowDirect": False,
        "chatbotUrl": common.CHATBOT_VOICE_URL,
        # Legacy field kept for the verify harness and older clients.
        "s2sUrl": common.CHATBOT_VOICE_URL,
        "startupGreeting": common.STARTUP_GREETING,
        "desktopControl": desktop._desktop_control_available(),
        "webPort": common.WEB_PORT,
        **common.SOURCE_SNAPSHOT.describe(),
    }


@app.get("/")
def index() -> dict:
    """No browser UI remains; the native macOS app uses /api/* on this sidecar."""
    return {"ok": True, "service": "chatbot-sidecar", "ui": "removed"}
