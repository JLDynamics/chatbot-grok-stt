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

import gc
import sys
import types
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


# ── Backward-compatible ``server.*`` surface ──────────────────────────────
# This file used to define every helper and global above. Long-lived callers
# (notably tests outside this refactor's ownership) still read ``server.<name>``
# and monkeypatch ``server.<name>`` to steer the handlers. The canonical
# definitions now live in the concern modules; the map below resolves each
# legacy name to its owning module exactly once, so there is still a single
# writer for every piece of mutable state.
#
# Reads go through module ``__getattr__`` (PEP 562) so they always see the
# live object; writes are forwarded by ``_CompatModule.__setattr__`` so a
# patch applied to ``server`` lands on the owning module where the route
# handlers actually look. Names defined in this file (``app``, ``config``,
# ``index``, the ``web_app.*`` module aliases) take precedence: normal module
# attributes win over ``__getattr__`` and non-compat writes behave as usual.
_CONCERN_MODULES = (common, browser, search, fetch, read_page, desktop, sessions)
# Per-module implementation details that must NOT leak onto ``server``: the
# routers (served via ``app``), per-module loggers, and aliases to sibling
# ``web_app`` modules used for cross-concern calls.
_NON_COMPAT = {"router", "logger", "common", "browser_store", "fetch_module"}
_COMPAT: dict[str, types.ModuleType] = {}
for _module in _CONCERN_MODULES:
    for _name in vars(_module):
        if _name.startswith("__") or _name in _NON_COMPAT or _name in _COMPAT:
            continue
        _COMPAT[_name] = _module
del _module, _name


class _CompatModule(types.ModuleType):
    """A module that forwards legacy ``server.*`` names to their owner."""

    def __getattr__(self, name: str) -> object:
        try:
            return getattr(_COMPAT[name], name)
        except KeyError:
            raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None

    def __setattr__(self, name: str, value: object) -> None:
        owner = _COMPAT.get(name)
        if owner is not None:
            setattr(owner, name, value)
        else:
            super().__setattr__(name, value)


def _own_module() -> types.ModuleType:
    """The module object currently executing this file.

    ``import web_app.server`` registers it in ``sys.modules`` before exec,
    but the tests load this file via ``spec_from_file_location`` +
    ``exec_module`` (which never registers), so fall back to finding the
    module whose namespace ``is`` this one.
    """
    candidate = sys.modules.get(__name__)
    if isinstance(candidate, types.ModuleType) and candidate.__dict__ is globals():
        return candidate
    for referrer in gc.get_referrers(globals()):
        if isinstance(referrer, types.ModuleType) and referrer.__dict__ is globals():
            return referrer
    raise RuntimeError(f"cannot locate module object for {__name__!r}")


_own_module().__class__ = _CompatModule
