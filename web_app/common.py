"""Shared sidecar plumbing: config, pooled HTTP client, and URL safety helpers.

Every other ``web_app`` module imports from here. This module owns the
process-wide mutable state (the pooled :class:`httpx.AsyncClient`) and the
read-only environment configuration, so no two modules can disagree about
them.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI

from chatbot.build_info import SIDECAR_SOURCES, SourceSnapshot

logger = logging.getLogger("chatbot.sidecar")
logger.setLevel(logging.INFO)
# What this process loaded, so /api/config can say when disk has moved on.
SOURCE_SNAPSHOT = SourceSnapshot(SIDECAR_SOURCES)

CHATBOT_VOICE_URL = os.environ.get(
    "CHATBOT_VOICE_URL", os.environ.get("SPEECH_TO_SPEECH_URL", "ws://localhost:8766/v1/realtime")
).strip()
STARTUP_GREETING = os.environ.get("STARTUP_GREETING", "").strip()
SERPER_KEY = os.environ.get("SERPER_API_KEY", "").strip()
TAVILY_KEY = os.environ.get("TAVILY_API_KEY", "").strip()
TINYFISH_KEY = os.environ.get("TINYFISH_API_KEY", "").strip()
SERPER_URL = "https://google.serper.dev/search"
TAVILY_URL = "https://api.tavily.com/search"
TINYFISH_SEARCH_URL = "https://api.search.tinyfish.ai/"
TINYFISH_FETCH_URL = "https://api.fetch.tinyfish.ai"
MAX_RESULTS = 5
FETCH_MAX_BYTES = 2_000_000
# Enough for a long article; beyond this the model pays prefill time on every
# follow-up turn for text it will never quote.
FETCH_MAX_CHARS = 16_000
FETCH_TIMEOUT_S = 15.0
SEARCH_TIMEOUT_S = 12.0

_http: httpx.AsyncClient | None = None


def _client() -> httpx.AsyncClient:
    """Shared pooled client for the search/fetch providers.

    Keeping connections alive saves a TCP+TLS handshake (100-300 ms) on every
    tool call, which is most of the difference between a search that feels
    instant and one the user notices.
    """
    global _http
    if _http is None or _http.is_closed:
        _http = httpx.AsyncClient(
            timeout=httpx.Timeout(FETCH_TIMEOUT_S, connect=5.0),
            follow_redirects=False,
            headers={"User-Agent": "Mozilla/5.0 (compatible; chatbot/1.0)"},
            limits=httpx.Limits(max_keepalive_connections=8, max_connections=16),
        )
    return _http


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    yield
    global _http
    if _http is not None and not _http.is_closed:
        await _http.aclose()
    _http = None


def _env_float(name: str, default: float) -> float:
    """Read a numeric env knob without letting garbage kill the sidecar at import."""
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        logger.warning("Ignoring invalid %s; using %s", name, default)
        return default


WEB_PORT = int(_env_float("WEB_PORT", _env_float("PORT_WEB", 7860.0)))


def _is_public_url(url: str) -> tuple[bool, str]:
    """Prevent model-facing web tools from accepting local or private networks."""
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"}:
        return False, "Only http and https URLs can be fetched."
    host = parts.hostname
    if not host:
        return False, "That URL has no host."
    if host.rstrip(".").lower() in {"localhost", "localhost.localdomain"}:
        return False, "Refusing to fetch a local address."
    try:
        addresses = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False, f"Could not resolve {host}."
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address[4][0])
        except ValueError:
            continue
        if (
            ip.is_loopback
            or ip.is_private
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            return False, "Refusing to fetch a private or loopback address."
    return True, ""


# Statuses a site returns when it is refusing an anonymous reader rather than
# failing: unauthorized, payment required, forbidden, rate limited.
GATED_STATUS_CODES = {401, 402, 403, 429}
# Only used to qualify an already-suspiciously-short page, never on its own.
GATED_TEXT_MARKERS = (
    "subscribe to continue",
    "subscribers only",
    "create a free account",
    "sign in to read",
    "log in to continue",
    "this content is for subscribers",
    "enable javascript",
    "verify you are human",
    "checking your browser",
)
GATED_TEXT_MAX_CHARS = 900


def _looks_gated(status_code: int, text: str) -> str | None:
    """Why this page looks withheld rather than read, or None.

    Deliberately conservative: a status code is trustworthy on its own, but
    prose markers only count on a page too short to be the real article. A
    false positive sends the caller up the fallback chain for no reason.
    """
    if status_code in GATED_STATUS_CODES:
        return f"http_{status_code}"
    stripped = text.strip()
    if len(stripped) <= GATED_TEXT_MAX_CHARS:
        lowered = stripped.lower()
        for marker in GATED_TEXT_MARKERS:
            if marker in lowered:
                return "paywall_or_interstitial"
    return None
