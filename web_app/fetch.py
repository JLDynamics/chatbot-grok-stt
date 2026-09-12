"""Fetch concern: the single HTTP + text-extraction + gating implementation.

Both ``/api/fetch`` and the read-page ladder (``read_page.py``) funnel
through :func:`_fetch`; there is exactly one copy of the redirect
re-validation, HTML reduction, and paywall-detection logic.
"""

from __future__ import annotations

import asyncio
import logging
import re
from html.parser import HTMLParser
from urllib.parse import urljoin

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from web_app import common

logger = logging.getLogger("chatbot.sidecar")

router = APIRouter()


class _TextExtractor(HTMLParser):
    """Reduce a readable HTML page to bounded plain text without extra dependencies."""

    SKIP = {"script", "style", "noscript", "svg", "head", "nav", "footer", "form"}
    BREAK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article"}
    MAIN = {"main", "article"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._main_depth = 0
        self._parts: list[str] = []
        self._main_parts: list[str] = []
        self._in_title = False
        self.title: str | None = None

    def _sink(self) -> list[str]:
        return self._main_parts if self._main_depth else self._parts

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.SKIP:
            self._skip_depth += 1
            return
        if tag in self.MAIN or dict(attrs).get("role") == "main":
            self._main_depth += 1
            return
        if tag == "title":
            self._in_title = True
        elif tag in self.BREAK:
            self._sink().append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP and self._skip_depth:
            self._skip_depth -= 1
            return
        if tag in self.MAIN and self._main_depth:
            self._main_depth -= 1
            return
        if tag == "title":
            self._in_title = False
        elif tag in self.BREAK:
            self._sink().append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title and self.title is None:
            self.title = data.strip() or None
        if self._skip_depth:
            return
        text = data.strip()
        if text:
            self._sink().append(text + " ")

    @staticmethod
    def _clean(parts: list[str]) -> str:
        raw = "".join(parts)
        raw = re.sub(r"[ \t]+", " ", raw)
        return re.sub(r"\n\s*\n\s*\n+", "\n\n", raw).strip()

    def text(self) -> str:
        main = self._clean(self._main_parts)
        return main if len(main) >= 200 else self._clean(self._parts)


class FetchRequest(BaseModel):
    url: str


async def _tinyfish_fetch(client: httpx.AsyncClient, url: str, key: str) -> dict:
    response = await client.post(
        common.TINYFISH_FETCH_URL,
        headers={"X-API-Key": key, "Content-Type": "application/json"},
        json={"urls": [url], "format": "markdown"},
    )
    if response.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Fetch provider error ({response.status_code}).")
    data = response.json()
    errors = data.get("errors") or []
    if errors:
        message = errors[0].get("message") or errors[0].get("error") or "Could not fetch that page."
        raise HTTPException(status_code=502, detail=str(message))
    results = data.get("results") or []
    if not results:
        raise HTTPException(status_code=502, detail="Fetch provider returned no content.")
    page = results[0]
    text = (page.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=502, detail="That page had no readable text.")
    truncated = len(text) > common.FETCH_MAX_CHARS
    # The provider already returned 200, so only the text markers can apply.
    # Both fetch paths must report this field or the caller cannot rely on it.
    gated_reason = common._looks_gated(200, text)
    return {
        "url": page.get("final_url") or page.get("url") or url,
        "title": page.get("title"),
        "text": text[: common.FETCH_MAX_CHARS],
        "truncated": truncated,
        "gated": gated_reason is not None,
        "gated_reason": gated_reason,
    }


async def _fetch(raw_url: str) -> dict:
    """Fetch one public text page; prefers TinyFish when configured.

    Raises :class:`HTTPException` with a model-readable ``detail`` on failure.
    """
    url = raw_url.strip()
    if not url:
        raise HTTPException(status_code=400, detail="No URL given.")
    if "://" not in url:
        url = "https://" + url
    allowed, reason = await asyncio.to_thread(common._is_public_url, url)
    if not allowed:
        raise HTTPException(status_code=400, detail=reason)
    client = common._client()
    if common.TINYFISH_KEY:
        try:
            return await _tinyfish_fetch(client, url, common.TINYFISH_KEY)
        except HTTPException as exc:
            if exc.status_code != 502:
                raise
            logger.warning("TinyFish fetch failed (%s); trying a direct HTTP fetch", exc.detail)
        except httpx.RequestError as exc:
            logger.warning("TinyFish fetch unreachable (%r); trying a direct HTTP fetch", exc)
    try:
        # Re-validate every redirect hop: the client must never follow a
        # public URL into a private or loopback address.
        response = None
        for _ in range(4):
            allowed, reason = await asyncio.to_thread(common._is_public_url, url)
            if not allowed:
                raise HTTPException(status_code=400, detail=reason)
            response = await client.get(url)
            if response.status_code not in (301, 302, 303, 307, 308):
                break
            location = response.headers.get("location", "")
            if not location:
                break
            url = urljoin(url, location)
        else:
            raise HTTPException(status_code=502, detail="That page redirected too many times.")
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail="Could not reach that page.") from exc
    assert response is not None

    content_type = response.headers.get("content-type", "")
    body = response.content[: common.FETCH_MAX_BYTES]
    if "html" in content_type:
        parser = _TextExtractor()
        try:
            parser.feed(body.decode(response.encoding or "utf-8", errors="replace"))
        except Exception as exc:
            raise HTTPException(status_code=502, detail="Could not parse that page.") from exc
        title, text = parser.title, parser.text()
    elif "text/" in content_type or "json" in content_type or "xml" in content_type:
        title, text = None, body.decode(response.encoding or "utf-8", errors="replace").strip()
    else:
        raise HTTPException(
            status_code=415,
            detail=f"That is not a readable page ({content_type or 'unknown type'}).",
        )
    truncated = len(text) > common.FETCH_MAX_CHARS
    gated_reason = common._looks_gated(response.status_code, text)
    return {
        "url": str(response.url),
        "title": title,
        "text": text[: common.FETCH_MAX_CHARS],
        "truncated": truncated,
        "gated": gated_reason is not None,
        "gated_reason": gated_reason,
    }


@router.post("/api/fetch")
async def fetch_page(req: FetchRequest) -> JSONResponse:
    """Fetch one public text page; prefers TinyFish when configured."""
    return JSONResponse(await _fetch(req.url))
