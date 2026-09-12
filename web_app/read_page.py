"""Read-page concern: one tool call that walks the text ladder.

The model asks for a page once; this endpoint walks the text ladder (web
fetch, then the live Chrome page via the bridge, or the other way round for
pages that only render logged in) and reports every attempt, so the caller
can say which method finally worked or exactly why none did.

The fetch rung reuses :func:`fetch._fetch` (the same implementation behind
``/api/fetch``); the bridge rung reads the store owned by ``browser.py``.
Neither rung duplicates HTTP, extraction, or gating logic.
"""

from __future__ import annotations

import json
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from web_app import browser as browser_store
from web_app import common
from web_app import fetch as fetch_module

router = APIRouter()

BROWSER_FIRST_HOSTS = {"x.com", "www.x.com", "mobile.x.com", "twitter.com", "www.twitter.com", "mobile.twitter.com"}
UNUSABLE_PAGE_STATUSES = {"wrong_page", "failed", "blocked", "unavailable", "error"}


class ReadPageRequest(BaseModel):
    url: str | None = None
    prefer_browser: bool = False


def _prefers_browser(url: str) -> bool:
    return (urlsplit(url).hostname or "").lower() in BROWSER_FIRST_HOSTS


def _same_page(left: str, right: str) -> bool:
    def normalized(raw: str) -> str | None:
        parts = urlsplit(raw)
        host = (parts.hostname or "").lower()
        if parts.scheme.lower() not in {"http", "https"} or not host:
            return None
        if host in BROWSER_FIRST_HOSTS:
            host = "x.com"
        path = parts.path or "/"
        return f"{host}{path}?{parts.query}" if parts.query else f"{host}{path}"

    a, b = normalized(left), normalized(right)
    return a is not None and a == b


def _bridge_result(requested_url: str | None) -> dict:
    """The live Chrome page as a read_page result dict (never raises)."""
    page = browser_store._fresh_browser_page()
    if page is None:
        known = browser_store._last_seen_page()
        if known is None:
            return {
                "status": "failed",
                "reason": "bridge_never_enabled",
                "message": (
                    "No page has been shared from Chrome. Click the Chatbot Page Bridge "
                    "toolbar icon once on the tab you want read."
                ),
            }
        url, title = known
        return {
            "status": "failed",
            "reason": "bridge_expired",
            "message": (
                "The shared copy of that page has expired. Click the Chatbot Page Bridge toolbar icon once to refresh it."
            ),
            "url": url,
            "title": title,
        }
    if requested_url and not _same_page(requested_url, page.url):
        # Never substitute a different open page for the one that was asked for.
        return {
            "status": "wrong_page",
            "reason": "wrong_page",
            "message": "Chrome has a different page open than the one requested.",
            "open_url": page.url,
        }
    gated_reason = common._looks_gated(200, page.text)
    return {
        "status": "blocked" if gated_reason else "read",
        "source": "chrome_bridge",
        "url": page.url,
        "title": page.title,
        "text": page.text,
        "content_type": page.content_type,
        "truncated": page.truncated,
        "complete": False if gated_reason else page.complete,
        "gated": gated_reason is not None,
        "gated_reason": gated_reason,
    }


async def _fetch_result(url: str) -> dict:
    """``fetch._fetch`` as a read_page result dict (never raises)."""
    try:
        data = await fetch_module._fetch(url)
    except HTTPException as exc:
        detail = exc.detail
        return {
            "status": "failed",
            "message": detail if isinstance(detail, str) else json.dumps(detail),
            "url": url,
        }
    data["status"] = "blocked" if data.get("gated") else "read"
    data["source"] = "web_fetch"
    return data


@router.post("/api/read_page")
async def read_page(req: ReadPageRequest) -> JSONResponse:
    url = (req.url or "").strip() or None
    if url and "://" not in url:
        url = "https://" + url
    browser_first = req.prefer_browser or url is None or _prefers_browser(url)
    methods = ["chrome_bridge", "web_fetch"] if browser_first else ["web_fetch", "chrome_bridge"]
    attempts: list[dict] = []
    partial: dict | None = None
    known_url = url
    for method in methods:
        if method == "web_fetch":
            if known_url is None:
                continue
            result = await _fetch_result(known_url)
        else:
            result = _bridge_result(url)
            if known_url is None and result.get("url"):
                # An expired bridge still remembers where the user was; that
                # address makes the fetch rung possible.
                known_url = str(result["url"])
        text = (result.get("text") or "").strip()
        status = str(result.get("status") or "")
        usable = bool(text) and not result.get("gated") and status not in UNUSABLE_PAGE_STATUSES
        complete = usable and not result.get("truncated") and bool(result.get("complete", True))
        attempts.append(
            {
                "method": method,
                "status": "read" if complete else "partial" if usable else (status or "failed"),
                "reason": result.get("reason") or result.get("gated_reason") or result.get("message") or "",
            }
        )
        if usable:
            result["source"] = method
            result["complete"] = complete
            if complete:
                result["status"] = "read"
                result["attempts"] = attempts
                return JSONResponse(result)
            # Keep useful partial text while trying the other method for a
            # complete page. Sources are not concatenated: that duplicates text
            # or mixes page revisions.
            if partial is None:
                partial = result
    if partial is not None:
        partial["status"] = "partial"
        partial["attempts"] = attempts
        return JSONResponse(partial)
    failure: dict = {
        "status": "unavailable",
        "complete": False,
        "attempts": attempts,
        "message": (
            "Neither the web fetch nor the Chrome page bridge returned that page. If it is open in "
            "Chrome, clicking the Chatbot Page Bridge toolbar icon once shares it."
        ),
    }
    if known_url:
        failure["url"] = known_url
    return JSONResponse(failure)
