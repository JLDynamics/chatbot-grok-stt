"""Chrome page-bridge concern: storing and serving pages shared from the extension.

Owns the bridge mutable state (``browser_pages`` and ``last_seen_pages``);
no other module may mutate these dicts.
"""

from __future__ import annotations

import re
import time
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from web_app import common

router = APIRouter()


class BrowserPage(BaseModel):
    bridge_version: str = ""
    tab_id: str = ""
    url: str
    article_id: str = ""
    title: str = ""
    text: str
    source: str = "browser_dom"
    content_type: str = "article"
    complete: bool = False
    truncated: bool = False
    clutter_filtered: bool = False
    comments_excluded: bool = False
    replies_detected: bool = False
    boundary: str = ""


BROWSER_PAGE_TTL_S = 300.0
BROWSER_PAGE_MIN_CHARS = 200
BROWSER_PAGE_MAX_CHARS = 60_000
BROWSER_PAGE_MAX_ENTRIES = 50
BROWSER_PAGE_MAX_BODY_BYTES = 100_000
# How long a tab's *address* stays known after its text has expired. The text
# is the sensitive payload and keeps the short TTL above; remembering only
# where the user was lets a failed bridge read name the page so the caller can
# fall back to fetching it. Cleared by /api/browser/hide and /api/browser/clear.
BROWSER_URL_TTL_S = 1800.0
browser_pages: dict[str, tuple[float, BrowserPage]] = {}
# tab key -> (seen_at, url, title). Never holds page text.
last_seen_pages: dict[str, tuple[float, str, str]] = {}


class BrowserPageHide(BaseModel):
    tab_id: str


def _validate_browser_page(page: BrowserPage) -> None:
    if page.tab_id and not re.fullmatch(r"[1-9][0-9]{0,19}", page.tab_id):
        raise HTTPException(status_code=400, detail="The browser tab identifier is invalid.")
    parsed = urlsplit(page.url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or not host:
        raise HTTPException(status_code=400, detail="Only HTTP(S) pages are accepted.")
    if host in {"127.0.0.1", "localhost"} and parsed.port == common.WEB_PORT:
        raise HTTPException(status_code=400, detail="The chatbot page cannot bridge itself.")
    public, reason = common._is_public_url(page.url)
    if not public:
        raise HTTPException(status_code=400, detail=reason)
    text = page.text.strip()
    minimum_chars = 1 if page.source == "x_post_dom" else BROWSER_PAGE_MIN_CHARS
    if len(text) < minimum_chars:
        raise HTTPException(status_code=400, detail="The page does not contain enough main text.")
    if len(text) > BROWSER_PAGE_MAX_CHARS:
        raise HTTPException(status_code=413, detail="The page text is too large.")
    if page.source == "x_dom":
        if host not in {"x.com", "www.x.com"}:
            raise HTTPException(status_code=400, detail="The X Article host is invalid.")
        if (
            not page.complete
            or not page.comments_excluded
            or page.boundary
            not in {
                "x_article_body_end",
                "x_focus_article_end",
            }
        ):
            raise HTTPException(status_code=400, detail="The X Article boundary is incomplete.")
    elif page.source == "x_post_dom":
        if (
            host not in {"x.com", "www.x.com"}
            or page.content_type != "x_post"
            or not page.complete
            or not page.comments_excluded
            or page.boundary != "x_primary_post_end"
            or not re.search(r"/[^/]+/status/[0-9]+(?:/|$)", parsed.path)
        ):
            raise HTTPException(status_code=400, detail="The X post boundary is incomplete.")
    elif (
        page.source != "browser_dom"
        or page.content_type not in {"article", "main", "page"}
        or page.boundary not in {"semantic_article_end", "main_content_end", "document_body_end"}
    ):
        raise HTTPException(status_code=400, detail="The page boundary is incomplete.")


def _store_browser_page(page: BrowserPage) -> dict:
    _validate_browser_page(page)
    if page.tab_id:
        # An already-running server can briefly contain entries posted by the
        # pre-0.2.1 extension, which had no tab identity and cannot be hidden.
        # Once the upgraded extension checks in, discard those legacy entries
        # so they cannot resurface after the visible tab is hidden.
        for key, (_, cached) in list(browser_pages.items()):
            if not cached.tab_id:
                browser_pages.pop(key, None)
    key = page.tab_id or page.article_id.strip() or page.url
    browser_pages[key] = (time.monotonic(), page)
    last_seen_pages[key] = (time.monotonic(), page.url, page.title)
    while len(browser_pages) > BROWSER_PAGE_MAX_ENTRIES:
        oldest = min(browser_pages, key=lambda key: browser_pages[key][0])
        browser_pages.pop(oldest, None)
    while len(last_seen_pages) > BROWSER_PAGE_MAX_ENTRIES:
        oldest = min(last_seen_pages, key=lambda key: last_seen_pages[key][0])
        last_seen_pages.pop(oldest, None)
    return {
        "ok": True,
        "chars": len(page.text),
        "complete": page.complete,
        "truncated": page.truncated,
        "content_type": page.content_type,
        "boundary": page.boundary,
        "bridge_version": page.bridge_version,
    }


def _last_seen_page(now: float | None = None) -> tuple[str, str] | None:
    """Most recent (url, title) still inside BROWSER_URL_TTL_S, text aside."""
    now = time.monotonic() if now is None else now
    for key, (seen_at, _url, _title) in list(last_seen_pages.items()):
        if now - seen_at > BROWSER_URL_TTL_S:
            last_seen_pages.pop(key, None)
    if not last_seen_pages:
        return None
    _seen_at, url, title = max(last_seen_pages.values(), key=lambda item: item[0])
    return url, title


def _fresh_browser_page(now: float | None = None) -> BrowserPage | None:
    entry = _fresh_browser_page_entry(now)
    return entry[0] if entry else None


def _fresh_browser_page_entry(now: float | None = None) -> tuple[BrowserPage, float] | None:
    now = time.monotonic() if now is None else now
    for key, (seen_at, _) in list(browser_pages.items()):
        if now - seen_at > BROWSER_PAGE_TTL_S:
            browser_pages.pop(key, None)
    if not browser_pages:
        return None
    seen_at, page = max(browser_pages.values(), key=lambda item: item[0])
    return page, max(0.0, now - seen_at)


@router.post("/api/browser/page")
async def browser_page(request: Request) -> dict:
    if request.headers.get("x-chatbot-bridge") != "page-v1":
        raise HTTPException(status_code=403, detail="Missing browser bridge header.")
    try:
        content_length = int(request.headers.get("content-length") or 0)
    except ValueError:
        raise HTTPException(status_code=400, detail="The browser payload size is invalid.")
    if content_length <= 0 or content_length > BROWSER_PAGE_MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="The browser payload is too large.")
    page = BrowserPage.model_validate(await request.json())
    return _store_browser_page(page)


@router.post("/api/browser/hide")
async def hide_browser_page(request: Request) -> dict:
    """Discard a tab as soon as Chrome reports that it is no longer visible."""
    if request.headers.get("x-chatbot-bridge") != "page-v1":
        raise HTTPException(status_code=403, detail="Missing browser bridge header.")
    hidden = BrowserPageHide.model_validate(await request.json())
    if not re.fullmatch(r"[1-9][0-9]{0,19}", hidden.tab_id):
        raise HTTPException(status_code=400, detail="The browser tab identifier is invalid.")
    removed = browser_pages.pop(hidden.tab_id, None) is not None
    # "This tab is no longer visible" must forget the address too, otherwise
    # hiding a page would still leave it nameable.
    last_seen_pages.pop(hidden.tab_id, None)
    return {"ok": True, "removed": removed}


@router.post("/api/browser/clear")
async def clear_browser_pages(request: Request) -> dict:
    """Discard all bridged text when the session is disabled or page is unsafe."""
    if request.headers.get("x-chatbot-bridge") != "page-v1":
        raise HTTPException(status_code=403, detail="Missing browser bridge header.")
    removed = len(browser_pages)
    browser_pages.clear()
    last_seen_pages.clear()
    return {"ok": True, "removed": removed}


@router.post("/api/browser/read")
async def read_browser_page() -> JSONResponse:
    """Return the fresh, read-only page supplied by the Chrome extension.

    A miss returns 503 with a machine-readable ``reason`` and, when the tab's
    address is still remembered, the ``url``. Callers need both to choose a
    fallback: "never enabled" and "expired" want different advice, and only a
    known URL makes fetching the page an option.
    """
    page = _fresh_browser_page()
    if page is None:
        known = _last_seen_page()
        if known is None:
            raise HTTPException(
                status_code=503,
                detail={
                    "reason": "bridge_never_enabled",
                    "message": (
                        "No page has been shared from Chrome. Click the Chatbot Page Bridge "
                        "toolbar icon once on the tab you want read."
                    ),
                },
            )
        url, title = known
        raise HTTPException(
            status_code=503,
            detail={
                "reason": "bridge_expired",
                "message": (
                    "The shared copy of that page has expired. Click the Chatbot Page Bridge "
                    "toolbar icon once to refresh it."
                ),
                "url": url,
                "title": title,
            },
        )
    return JSONResponse(
        {
            "method": "text",
            "source": "chrome_bridge",
            "info": {"frontmost": {"app": "Google Chrome", "title": page.title}},
            **page.model_dump(),
        }
    )


@router.get("/api/browser/status")
async def browser_bridge_status() -> JSONResponse:
    """Return bridge freshness/version metadata without exposing page text."""
    entry = _fresh_browser_page_entry()
    if not entry:
        return JSONResponse(
            {"connected": False, "expected_version": "0.4.3", "web_port": common.WEB_PORT}
        )
    page, age_s = entry
    return JSONResponse(
        {
            "connected": True,
            "expected_version": "0.4.3",
            "web_port": common.WEB_PORT,
            "bridge_version": page.bridge_version or "legacy",
            "age_ms": round(age_s * 1000),
            "content_type": page.content_type,
            "host": (urlsplit(page.url).hostname or "").lower(),
        }
    )
