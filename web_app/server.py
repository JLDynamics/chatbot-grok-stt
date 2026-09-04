"""Local API sidecar for the native macOS app: search, memory, sessions, code, and desktop tools."""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import json
import logging
import os
import re
import signal
import socket
import tempfile
import time
import uuid
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

app = FastAPI()
logger = logging.getLogger("chatbot.sidecar")
logger.setLevel(logging.INFO)

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
FETCH_MAX_CHARS = 20_000
FETCH_TIMEOUT_S = 15.0


def _env_float(name: str, default: float) -> float:
    """Read a numeric env knob without letting garbage kill the sidecar at import."""
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        logger.warning("Ignoring invalid %s; using %s", name, default)
        return default


WEB_PORT = int(_env_float("WEB_PORT", _env_float("PORT_WEB", 7860.0)))


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
browser_pages: dict[str, tuple[float, BrowserPage]] = {}


class BrowserPageHide(BaseModel):
    tab_id: str


def _validate_browser_page(page: BrowserPage) -> None:
    if page.tab_id and not re.fullmatch(r"[1-9][0-9]{0,19}", page.tab_id):
        raise HTTPException(status_code=400, detail="The browser tab identifier is invalid.")
    parsed = urlsplit(page.url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or not host:
        raise HTTPException(status_code=400, detail="Only HTTP(S) pages are accepted.")
    if host in {"127.0.0.1", "localhost"} and parsed.port == WEB_PORT:
        raise HTTPException(status_code=400, detail="The chatbot page cannot bridge itself.")
    public, reason = _is_public_url(page.url)
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
    browser_pages[page.tab_id or page.article_id.strip() or page.url] = (time.monotonic(), page)
    while len(browser_pages) > BROWSER_PAGE_MAX_ENTRIES:
        oldest = min(browser_pages, key=lambda key: browser_pages[key][0])
        browser_pages.pop(oldest, None)
    return {
        "ok": True,
        "chars": len(page.text),
        "complete": page.complete,
        "truncated": page.truncated,
        "content_type": page.content_type,
        "boundary": page.boundary,
        "bridge_version": page.bridge_version,
    }


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


@app.post("/api/browser/page")
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


@app.post("/api/browser/hide")
async def hide_browser_page(request: Request) -> dict:
    """Discard a tab as soon as Chrome reports that it is no longer visible."""
    if request.headers.get("x-chatbot-bridge") != "page-v1":
        raise HTTPException(status_code=403, detail="Missing browser bridge header.")
    hidden = BrowserPageHide.model_validate(await request.json())
    if not re.fullmatch(r"[1-9][0-9]{0,19}", hidden.tab_id):
        raise HTTPException(status_code=400, detail="The browser tab identifier is invalid.")
    removed = browser_pages.pop(hidden.tab_id, None) is not None
    return {"ok": True, "removed": removed}


@app.post("/api/browser/clear")
async def clear_browser_pages(request: Request) -> dict:
    """Discard all bridged text when the session is disabled or page is unsafe."""
    if request.headers.get("x-chatbot-bridge") != "page-v1":
        raise HTTPException(status_code=403, detail="Missing browser bridge header.")
    removed = len(browser_pages)
    browser_pages.clear()
    return {"ok": True, "removed": removed}


@app.post("/api/browser/read")
async def read_browser_page() -> JSONResponse:
    """Return the fresh, read-only page supplied by the Chrome extension."""
    page = _fresh_browser_page()
    if page is None:
        raise HTTPException(status_code=503, detail="Open or reload the Chrome page and try again.")
    return JSONResponse(
        {
            "method": "text",
            "source": "chrome_bridge",
            "info": {"frontmost": {"app": "Google Chrome", "title": page.title}},
            **page.model_dump(),
        }
    )


@app.get("/api/browser/status")
async def browser_bridge_status() -> JSONResponse:
    """Return bridge freshness/version metadata without exposing page text."""
    entry = _fresh_browser_page_entry()
    if not entry:
        return JSONResponse({"connected": False, "expected_version": "0.4.3", "web_port": WEB_PORT})
    page, age_s = entry
    return JSONResponse(
        {
            "connected": True,
            "expected_version": "0.4.3",
            "web_port": WEB_PORT,
            "bridge_version": page.bridge_version or "legacy",
            "age_ms": round(age_s * 1000),
            "content_type": page.content_type,
            "host": (urlsplit(page.url).hostname or "").lower(),
        }
    )


@app.get("/api/config")
def config() -> dict:
    return {
        "search": bool(TINYFISH_KEY or SERPER_KEY or TAVILY_KEY),
        "allowDirect": False,
        "chatbotUrl": CHATBOT_VOICE_URL,
        # Legacy field kept for the verify harness and older clients.
        "s2sUrl": CHATBOT_VOICE_URL,
        "startupGreeting": STARTUP_GREETING,
        "codeAgent": CODE_AGENT_ENABLED,
        "desktopControl": _desktop_control_available(),
        "webPort": WEB_PORT,
    }


class SearchRequest(BaseModel):
    query: str
    key: str | None = None


def _clean_key(value: str | None) -> str:
    key = (value or "").strip().strip("'\"")
    if "=" in key and key.split("=", 1)[0].strip().endswith("API_KEY"):
        key = key.split("=", 1)[1].strip().strip("'\"")
    return key.split()[0] if key.split() else ""


def _resolve_search_key(user_key: str) -> str:
    return user_key or TINYFISH_KEY or SERPER_KEY or TAVILY_KEY


def _search_provider(key: str) -> str:
    if key.startswith("tvly-"):
        return "tavily"
    if key.startswith("sk-tinyfish-"):
        return "tinyfish"
    return "serper"


async def _tinyfish_search(client: httpx.AsyncClient, query: str, key: str) -> dict:
    response = await client.get(
        TINYFISH_SEARCH_URL,
        params={"query": query},
        headers={"X-API-Key": key},
    )
    if response.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Search provider error ({response.status_code}).")
    data = response.json()
    results = [
        {
            "title": item.get("title", ""),
            "snippet": (item.get("snippet") or "")[:300],
            "url": item.get("url", ""),
        }
        for item in (data.get("results") or [])[:MAX_RESULTS]
    ]
    return {"query": query, "answer": None, "results": results}


async def _tavily_search(client: httpx.AsyncClient, query: str, key: str) -> dict:
    response = await client.post(
        TAVILY_URL,
        headers={"Authorization": f"Bearer {key}"},
        json={"query": query, "max_results": MAX_RESULTS, "include_answer": True},
    )
    if response.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Search provider error ({response.status_code}).")
    data = response.json()
    results = [
        {"title": item.get("title", ""), "snippet": (item.get("content") or "")[:300], "url": item.get("url", "")}
        for item in (data.get("results") or [])[:MAX_RESULTS]
    ]
    return {"query": query, "answer": data.get("answer") or None, "results": results}


async def _serper_search(client: httpx.AsyncClient, query: str, key: str) -> dict:
    response = await client.post(
        SERPER_URL,
        headers={"X-API-KEY": key},
        json={"q": query, "num": MAX_RESULTS},
    )
    if response.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Search provider error ({response.status_code}).")
    data = response.json()
    results = [
        {"title": item.get("title", ""), "snippet": item.get("snippet", ""), "url": item.get("link", "")}
        for item in (data.get("organic") or [])[:MAX_RESULTS]
    ]
    answer_box = data.get("answerBox") or {}
    answer = answer_box.get("answer") or answer_box.get("snippet")
    if not answer:
        answer = (data.get("knowledgeGraph") or {}).get("description")
    return {"query": query, "answer": answer, "results": results}


async def _tinyfish_fetch(client: httpx.AsyncClient, url: str, key: str) -> dict:
    response = await client.post(
        TINYFISH_FETCH_URL,
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
    truncated = len(text) > FETCH_MAX_CHARS
    return {
        "url": page.get("final_url") or page.get("url") or url,
        "title": page.get("title"),
        "text": text[:FETCH_MAX_CHARS],
        "truncated": truncated,
    }


@app.post("/api/search")
async def search(req: SearchRequest) -> JSONResponse:
    query = req.query.strip()
    if not query:
        raise HTTPException(status_code=400, detail="Empty query.")
    key = _resolve_search_key(_clean_key(req.key))
    if not key:
        raise HTTPException(status_code=503, detail="Search is not configured.")
    provider = _search_provider(key)
    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            if provider == "tinyfish":
                payload = await _tinyfish_search(client, query, key)
            elif provider == "tavily":
                payload = await _tavily_search(client, query, key)
            else:
                payload = await _serper_search(client, query, key)
    except HTTPException:
        raise
    except httpx.RequestError as exc:
        logger.warning("Search provider unavailable: %r", exc)
        raise HTTPException(status_code=502, detail="Search provider unreachable.") from exc
    return JSONResponse(payload)


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
        if ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            return False, "Refusing to fetch a private or loopback address."
    return True, ""


class FetchRequest(BaseModel):
    url: str


@app.post("/api/fetch")
async def fetch_page(req: FetchRequest) -> JSONResponse:
    """Fetch one public text page; prefers TinyFish when configured."""
    url = req.url.strip()
    if not url:
        raise HTTPException(status_code=400, detail="No URL given.")
    if "://" not in url:
        url = "https://" + url
    allowed, reason = await asyncio.to_thread(_is_public_url, url)
    if not allowed:
        raise HTTPException(status_code=400, detail=reason)
    if TINYFISH_KEY:
        try:
            async with httpx.AsyncClient(timeout=FETCH_TIMEOUT_S) as client:
                return JSONResponse(await _tinyfish_fetch(client, url, TINYFISH_KEY))
        except HTTPException:
            raise
        except httpx.RequestError as exc:
            raise HTTPException(status_code=502, detail="Could not reach the fetch provider.") from exc
    try:
        async with httpx.AsyncClient(
            timeout=FETCH_TIMEOUT_S,
            follow_redirects=False,
            headers={"User-Agent": "Mozilla/5.0 (compatible; chatbot/1.0)"},
        ) as client:
            # Re-validate every redirect hop: the client must never follow a
            # public URL into a private or loopback address.
            response = None
            for _ in range(4):
                allowed, reason = await asyncio.to_thread(_is_public_url, url)
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

    content_type = response.headers.get("content-type", "")
    body = response.content[:FETCH_MAX_BYTES]
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
    truncated = len(text) > FETCH_MAX_CHARS
    return JSONResponse(
        {
            "url": str(response.url),
            "title": title,
            "text": text[:FETCH_MAX_CHARS],
            "truncated": truncated,
        }
    )

GROK_BIN = Path(os.path.expanduser("~/.local/bin/grok"))
CODE_AGENT_ENABLED = os.environ.get("CODE_AGENT", "on").lower() not in {"off", "0", "false"}
CODE_AGENT_CWD = Path(os.path.expanduser(os.environ.get("CODE_AGENT_CWD", "~")))
CODE_AGENT_MODEL = os.environ.get("CODE_AGENT_MODEL", "grok-4.6")
CODE_AGENT_TIMEOUT_S = _env_float("CODE_AGENT_TIMEOUT", 300.0)


class CodeRequest(BaseModel):
    task: str


@app.post("/api/code")
async def code_agent(req: CodeRequest) -> JSONResponse:
    task = req.task.strip()
    if not CODE_AGENT_ENABLED:
        raise HTTPException(status_code=503, detail="The coding agent is turned off.")
    if not task:
        raise HTTPException(status_code=400, detail="No task given.")
    if not GROK_BIN.exists():
        raise HTTPException(status_code=503, detail="Grok Build (grok) is not installed on ~/.local/bin.")
    logger.warning("code_agent: cwd=%s task_chars=%d model=%s", CODE_AGENT_CWD, len(task), CODE_AGENT_MODEL)
    try:
        process = await asyncio.create_subprocess_exec(
            str(GROK_BIN),
            "-p",
            task,
            "--model",
            CODE_AGENT_MODEL,
            "--cwd",
            str(CODE_AGENT_CWD),
            "--permission-mode",
            "bypassPermissions",
            "--output-format",
            "json",
            env=dict(os.environ),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        output, _ = await asyncio.wait_for(process.communicate(), timeout=CODE_AGENT_TIMEOUT_S)
    except asyncio.TimeoutError as exc:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            process.kill()
        await process.wait()
        raise HTTPException(status_code=504, detail="The coding agent timed out.") from exc
    except OSError as exc:
        raise HTTPException(status_code=502, detail="Could not start the coding agent.") from exc
    raw = output.decode("utf-8", errors="replace").strip()
    # --output-format json emits a single object with the response `text`.
    text = raw
    try:
        payload = json.loads(raw)
        if isinstance(payload, dict) and isinstance(payload.get("text"), str):
            text = payload["text"]
        elif isinstance(payload, list):
            text = "\n".join(str(x) for x in payload)
    except (json.JSONDecodeError, ValueError):
        pass
    if len(text) > 4000:
        text = text[:4000] + "\n[output truncated]"
    return JSONResponse({"ok": process.returncode == 0, "exit_code": process.returncode, "output": text})


DESKTOP_HARNESS_BIN = Path(os.path.expanduser("~/.local/bin/desktop-harness"))
DESKTOP_CONTROL_ENABLED = os.environ.get("DESKTOP_CONTROL", "on").lower() not in {"off", "0", "false"}
DESKTOP_CAPTURE_DIR = Path(tempfile.gettempdir()) / "desktop-harness"
DESKTOP_SCREENSHOT_MAX_BYTES = 8_000_000
DESKTOP_HARNESS_MAX_OUTPUT_BYTES = 64_000
LOGIN_HINTS = (
    "1password",
    "bitwarden",
    "keychain",
    "lastpass",
    "keepass",
    "dashlane",
    "nordpass",
    "enpass",
    "wallet",
    "bank",
    "sign in",
    "log in",
    "login",
    "password",
    "passcode",
    "two-factor",
    "authenticator",
    "checkout",
    "payment",
    "billing",
    "credit card",
    "card number",
    "security code",
    "verification code",
    "one-time code",
)
ACTIONS = {
    "click": "click_text({text!r}, app)",
    "type": "type_text({text!r})",
    "key": "key({text!r})",
    "hotkey": "hotkey(*{keys!r})",
    "scroll": "scroll(dy={dy!r})",
    "drag": "drag({x1!r}, {y1!r}, {x2!r}, {y2!r})",
}


def _desktop_control_available() -> bool:
    return DESKTOP_CONTROL_ENABLED and DESKTOP_HARNESS_BIN.is_file() and os.access(DESKTOP_HARNESS_BIN, os.X_OK)


class DesktopActRequest(BaseModel):
    action: str
    text: str | None = None
    app: str | None = None
    amount: int | None = None
    coords: list[float] | None = None


class ContextPreflightRequest(BaseModel):
    include_desktop: bool = False


async def _run_harness(script: str, timeout: float) -> tuple[int, str]:
    process = await asyncio.create_subprocess_exec(
        str(DESKTOP_HARNESS_BIN),
        "-c",
        script,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        # Run in-process (no warm daemon). The daemon auto-starts on first call
        # and writes to ~/Library/Caches/desktop-harness/daemon.log, which fails
        # when the web server can't write there, and screenshots captured in the
        # daemon can lack Screen Recording permission. In-process keeps desktop
        # actions/screenshots working regardless of daemon state (at the cost of
        # a cold pyobjc import per call).
        env={**os.environ, "DH_NO_DAEMON": "1"},
    )

    async def complete() -> tuple[int, bytes]:
        assert process.stdout is not None
        parts: list[bytes] = []
        size = 0
        while chunk := await process.stdout.read(4_096):
            size += len(chunk)
            if size > DESKTOP_HARNESS_MAX_OUTPUT_BYTES:
                process.kill()
                await process.wait()
                raise HTTPException(status_code=502, detail="Desktop Harness returned too much output.")
            parts.append(chunk)
        return await process.wait(), b"".join(parts)

    try:
        return_code, output = await asyncio.wait_for(complete(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        process.kill()
        await process.wait()
        raise HTTPException(status_code=504, detail="That action took too long.") from exc
    return return_code, output.decode("utf-8", errors="replace").strip()


async def _desktop_frontmost_context() -> dict[str, object]:
    """Return bounded app/window metadata only; never labels, pixels, or document text."""
    script = (
        "import json\n"
        "from desktop_harness.helpers import screen_info\n"
        "info = screen_info()\n"
        "front = info.get('frontmost') or {}\n"
        "pid = front.get('pid')\n"
        "windows = [w for w in info.get('windows', []) if w.get('pid') == pid]\n"
        "window = windows[0] if windows else {}\n"
        "print(json.dumps({'app': str(front.get('name') or '')[:120], "
        "'title': str(window.get('title') or '')[:200]}))\n"
    )
    code, output = await _run_harness(script, 8.0)
    if code != 0:
        raise HTTPException(status_code=502, detail="Could not inspect the current app context.")
    try:
        payload = json.loads(output.splitlines()[-1])
    except (json.JSONDecodeError, IndexError) as exc:
        raise HTTPException(status_code=502, detail="Desktop Harness returned invalid context metadata.") from exc
    app_name = str(payload.get("app") or "").strip()[:120]
    window_title = str(payload.get("title") or "").strip()[:200]
    sensitive = next((hint for hint in LOGIN_HINTS if hint in f"{app_name} {window_title}".lower()), None)
    if sensitive:
        return {"available": True, "sensitive": True, "app": "", "window_title": ""}
    return {
        "available": bool(app_name),
        "sensitive": False,
        "app": app_name,
        "window_title": window_title,
    }


@app.post("/api/context/preflight")
async def context_preflight(req: ContextPreflightRequest) -> JSONResponse:
    """Classify current context without returning page text or capturing the screen."""
    entry = _fresh_browser_page_entry()
    if entry:
        page, age_s = entry
        host = (urlsplit(page.url).hostname or "").lower()
        bridge = {
            "fresh_readable_page": True,
            "age_ms": round(age_s * 1000),
            "host": host,
            "title": page.title[:300],
            "content_type": page.content_type,
            "complete": page.complete,
            "truncated": page.truncated,
        }
    else:
        bridge = {"fresh_readable_page": False}

    desktop: dict[str, object] = {"inspected": False}
    if req.include_desktop and _desktop_control_available():
        desktop = {"inspected": True, **await _desktop_frontmost_context()}

    app_name = str(desktop.get("app") or "").lower()
    chrome_frontmost = app_name in {"google chrome", "chrome", "chromium"}
    if desktop.get("sensitive"):
        route_hint = "ask"
    elif bridge["fresh_readable_page"]:
        # Prefer the fast, text-based Chrome page bridge for reading page/article
        # content; desktop-harness / screenshot are the fallback, not the default.
        route_hint = "read_article"
    elif desktop.get("inspected") and desktop.get("available") and not chrome_frontmost:
        route_hint = "control_screen_screenshot"
    else:
        route_hint = "ask"

    return JSONResponse(
        {
            "purpose": "routing_only",
            "contains_page_text": False,
            "captured_screenshot": False,
            "authorization": {
                "public_page_text": "no_confirmation_required",
                "desktop_visual_or_action": "explicit_user_request_required",
            },
            "chrome_bridge": bridge,
            "desktop": desktop,
            "route_hint": route_hint,
        }
    )


async def _screen_scope_looks_sensitive(app: str | None = None) -> str | None:
    script = (
        "import json\n"
        "from desktop_harness.helpers import labels, screen_info\n"
        f"info = screen_info({app!r})\n"
        "try:\n"
        f"    info['labels'] = labels({app!r}, limit=60)\n"
        "except Exception as exc:\n"
        "    info['labels_error'] = str(exc)\n"
        "print(json.dumps(info))\n"
    )
    try:
        code, text = await _run_harness(script, 20.0)
        if code != 0:
            raise HTTPException(status_code=502, detail="Could not inspect the desktop safety scope.")
        info = json.loads(text.splitlines()[-1])
    except (json.JSONDecodeError, IndexError) as exc:
        raise HTTPException(status_code=502, detail="Could not inspect the desktop safety scope.") from exc
    scope = json.dumps(info).lower()
    return next((hint for hint in LOGIN_HINTS if hint in scope), None)


async def _capture_desktop_screenshot(app: str | None) -> dict[str, str]:
    script = (
        "import json\n"
        "from desktop_harness.helpers import screenshot\n"
        f"print(json.dumps({{'path': screenshot(app={app!r})}}))\n"
    )
    code, output = await _run_harness(script, 20.0)
    if code != 0:
        detail = output[-500:] or "Desktop Harness could not capture the screen."
        lowered = detail.lower()
        if "no on-screen window" in lowered:
            raise HTTPException(status_code=404, detail="No matching visible app or window was found.")
        if "screen recording" in lowered or "capture returned no image" in lowered:
            raise HTTPException(
                status_code=403,
                detail=(
                    "Screen capture is not available. Grant Screen Recording permission to the "
                    "app running Chatbot, then restart it."
                ),
            )
        raise HTTPException(status_code=502, detail=detail)
    try:
        payload = json.loads(output.splitlines()[-1])
        path = Path(payload["path"]).resolve(strict=True)
    except (json.JSONDecodeError, IndexError, KeyError, OSError, TypeError) as exc:
        raise HTTPException(status_code=502, detail="Desktop Harness returned an invalid screenshot path.") from exc
    capture_dir = DESKTOP_CAPTURE_DIR.resolve()
    if path.parent != capture_dir or not path.is_file():
        raise HTTPException(status_code=502, detail="Desktop Harness returned an unsafe screenshot path.")
    size = path.stat().st_size
    if size < 1_000:
        raise HTTPException(
            status_code=403,
            detail=(
                "Screen capture returned an empty image. Grant Screen Recording permission to the "
                "app running Chatbot, then restart it."
            ),
        )
    if size > DESKTOP_SCREENSHOT_MAX_BYTES:
        raise HTTPException(status_code=413, detail="The screenshot is too large to attach safely.")
    data = path.read_bytes()
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise HTTPException(status_code=502, detail="Desktop Harness did not return a valid PNG screenshot.")
    return {
        "image": "data:image/png;base64," + base64.b64encode(data).decode("ascii"),
        "path": str(path),
        "target": app or "main display",
    }


@app.post("/api/desktop/act")
async def desktop_act(req: DesktopActRequest) -> JSONResponse:
    if not DESKTOP_CONTROL_ENABLED:
        raise HTTPException(status_code=503, detail="Desktop control is turned off.")
    if not DESKTOP_HARNESS_BIN.is_file() or not os.access(DESKTOP_HARNESS_BIN, os.X_OK):
        raise HTTPException(status_code=503, detail="desktop-harness is not installed.")
    action = req.action.strip().lower()
    if action not in {*ACTIONS, "screenshot"}:
        raise HTTPException(status_code=400, detail=f"Unknown action {action!r}.")
    app_name = req.app.strip() if req.app else None
    if app_name and (len(app_name) > 120 or any(ord(char) < 32 for char in app_name)):
        raise HTTPException(status_code=400, detail="The app or window name is invalid.")
    sensitive_scope = app_name
    if action in {"click", "type", "key", "hotkey", "scroll", "drag", "screenshot"} and await _screen_scope_looks_sensitive(
        sensitive_scope
    ):
        raise HTTPException(status_code=451, detail="Desktop control is blocked on sign-in and payment windows.")
    if action == "screenshot":
        captured = await _capture_desktop_screenshot(app_name)
        return JSONResponse({"ok": True, "action": action, **captured})
    text = req.text or ""
    if action == "scroll":
        amount = req.amount if req.amount else 5
        expression = ACTIONS[action].format(dy=-abs(amount) if amount >= 0 else abs(amount))
    elif action == "drag":
        coords = req.coords or []
        if len(coords) != 4:
            raise HTTPException(status_code=400, detail="Drag needs coords [x1, y1, x2, y2].")
        expression = ACTIONS[action].format(x1=coords[0], y1=coords[1], x2=coords[2], y2=coords[3])
    elif action == "hotkey":
        keys = [key.lower() for key in text.replace("+", " ").split()]
        if not keys:
            raise HTTPException(status_code=400, detail="Hotkey needs keys.")
        expression = ACTIONS[action].format(keys=keys)
    else:
        if not text:
            raise HTTPException(status_code=400, detail=f"{action} needs text.")
        expression = ACTIONS[action].format(text=text)
    logger.warning("desktop_act: %s app=%r arg=%r", action, app_name, text[:120])
    script = (
        "import json\n"
        "from desktop_harness.helpers import click_text, type_text, key, hotkey, scroll, drag, labels, wait_stable\n"
        f"app = {app_name!r}\n"
        f"verify = {action in {'click', 'type', 'key', 'hotkey'}!r}\n"
        "before = set(labels(app, limit=60)) if verify else set()\n"
        f"result = {expression}\n"
        "if verify: wait_stable(0.35)\n"
        "changed = [x for x in labels(app, limit=60) if x and x not in before][:12] if verify else []\n"
        "print(json.dumps({'ok': True, 'result': str(result), 'changed': changed, 'verified': verify}))\n"
    )
    code, output = await _run_harness(script, 45.0)
    if code != 0:
        raise HTTPException(status_code=502, detail=output[-300:] or "Desktop action failed.")
    try:
        payload = json.loads(output.splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        payload = {"ok": True, "verified": False, "changed": [], "result": output[-500:]}
    return JSONResponse({"ok": True, "action": action, **payload})


# Durable conversation data lives beside the chatbot's existing local settings,
# never inside a code repository.  Full transcripts are the source of truth;
# the Markdown files are small, editable guides that can safely be included in
# a new model session.
CHATBOT_DATA = Path(os.path.expanduser(
    os.environ.get("CHATBOT_DATA_DIR", os.environ.get("S2S_DATA_DIR", "~/.chatbot"))
))
SESSIONS_DIR = CHATBOT_DATA / "sessions"
PERSONAL_MEMORY_PATH = CHATBOT_DATA / "personal-memory.md"
PERSONAL_MEMORY_MAX_CHARS = 10_000  # roughly 2,500 English-language tokens
SESSION_RETENTION_COUNT = max(1, int(_env_float("CHATBOT_SESSION_RETENTION", 50.0)))
history_lock = asyncio.Lock()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _atomic_write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(value, encoding="utf-8")
    temporary.replace(path)


def _read_json(path: Path, default: object) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _session_path(session_id: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{32}", session_id):
        raise HTTPException(status_code=400, detail="Invalid session.")
    return SESSIONS_DIR / f"{session_id}.json"


def _session_summary(session: dict) -> dict:
    messages = session.get("messages", [])
    preview = next((str(m.get("text", "")).strip() for m in messages if str(m.get("text", "")).strip()), "")
    return {
        "id": session.get("id"), "title": session.get("title", "New conversation"),
        "created_at": session.get("created_at"), "updated_at": session.get("updated_at"),
        "message_count": len(messages), "preview": preview[:180],
    }


def _load_session(session_id: str) -> dict:
    value = _read_json(_session_path(session_id), None)
    if not isinstance(value, dict):
        raise HTTPException(status_code=404, detail="Session not found.")
    value.setdefault("messages", [])
    return value


def _save_session(session: dict) -> None:
    _atomic_write(_session_path(str(session["id"])), json.dumps(session, indent=2, ensure_ascii=False))
    _prune_old_sessions()


def _session_recency_key(path: Path, session: dict) -> tuple[str, str, float, str]:
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0
    return (
        str(session.get("updated_at", "")),
        str(session.get("created_at", "")),
        mtime,
        str(session.get("id", "")),
    )


def _prune_old_sessions() -> None:
    if not SESSIONS_DIR.exists():
        return
    ranked: list[tuple[tuple[str, str, float, str], Path]] = []
    for path in SESSIONS_DIR.glob("*.json"):
        value = _read_json(path, None)
        if isinstance(value, dict):
            ranked.append((_session_recency_key(path, value), path))
    ranked.sort(key=lambda item: item[0], reverse=True)
    for _, path in ranked[SESSION_RETENTION_COUNT:]:
        path.unlink(missing_ok=True)


def _legacy_memory_paths() -> list[Path]:
    configured = os.environ.get("CHATBOT_MEMORIES_PATH", os.environ.get("S2S_MEMORIES_PATH", "")).strip()
    paths = [CHATBOT_DATA / "memories.json", CHATBOT_DATA / "memories.json.bak"]
    if configured:
        paths.insert(0, Path(os.path.expanduser(configured)))
    seen: set[Path] = set()
    unique: list[Path] = []
    for path in paths:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(path)
    return unique


def _read_legacy_memories(path: Path) -> list[dict]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []
    return value if isinstance(value, list) else []


def _dedupe_profile_lines(content: str) -> str:
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    seen: set[str] = set()
    unique: list[str] = []
    for line in lines:
        key = line.lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(line)
    return "\n".join(unique)


def _migrate_legacy_memories() -> None:
    """Fold legacy memories.json (and .bak) into personal-memory.md once."""
    profile = PERSONAL_MEMORY_PATH.read_text(encoding="utf-8") if PERSONAL_MEMORY_PATH.exists() else ""
    lines = [line for line in profile.splitlines() if line.strip()]
    existing = {line.strip().lower() for line in lines}
    added = False
    pending_renames: list[Path] = []
    for path in _legacy_memory_paths():
        if not path.exists():
            continue
        path_added = False
        for item in _read_legacy_memories(path):
            text = str(item.get("text", "")).strip()
            if not text:
                continue
            bullet = f"- {text}" if not text.startswith("-") else text
            if bullet.lower() in existing:
                continue
            lines.append(bullet)
            existing.add(bullet.lower())
            added = True
            path_added = True
        if path_added:
            pending_renames.append(path)
    if not added:
        return
    content = _dedupe_profile_lines("\n".join(lines))
    if not content:
        return
    if len(content) > PERSONAL_MEMORY_MAX_CHARS:
        return
    _atomic_write(PERSONAL_MEMORY_PATH, content + "\n")
    for path in pending_renames:
        migrated = path.with_name(f"{path.name}.migrated")
        try:
            path.rename(migrated)
        except OSError:
            path.unlink(missing_ok=True)


class SessionCreateRequest(BaseModel):
    title: str = "New conversation"


class SessionUpdateRequest(BaseModel):
    title: str | None = None
    messages: list[dict] | None = None


class ProfileUpdateRequest(BaseModel):
    content: str


@app.get("/api/sessions")
async def list_sessions() -> JSONResponse:
    async with history_lock:
        sessions = []
        for path in SESSIONS_DIR.glob("*.json") if SESSIONS_DIR.exists() else []:
            value = _read_json(path, None)
            if isinstance(value, dict):
                sessions.append(_session_summary(value))
        sessions.sort(key=lambda item: item.get("updated_at", ""), reverse=True)
    return JSONResponse({"sessions": sessions})


@app.post("/api/sessions")
async def create_session(req: SessionCreateRequest) -> JSONResponse:
    session = {"id": uuid.uuid4().hex, "title": req.title.strip()[:120] or "New conversation",
               "created_at": _utc_now(), "updated_at": _utc_now(), "messages": []}
    async with history_lock:
        _save_session(session)
    return JSONResponse({"session": session})


@app.get("/api/sessions/{session_id}")
async def get_session(session_id: str) -> JSONResponse:
    async with history_lock:
        return JSONResponse({"session": _load_session(session_id)})


@app.patch("/api/sessions/{session_id}")
async def update_session(session_id: str, req: SessionUpdateRequest) -> JSONResponse:
    async with history_lock:
        session = _load_session(session_id)
        if req.title is not None:
            session["title"] = req.title.strip()[:120] or "New conversation"
        if req.messages is not None:
            # Transcript messages are plain data, but each text is unbounded.
            # Cap every text so one broken client cannot fill the disk.
            capped = []
            for message in req.messages[-2000:]:
                message = dict(message)
                message["text"] = str(message.get("text", ""))[:20_000]
                capped.append(message)
            session["messages"] = capped
        session["updated_at"] = _utc_now()
        _save_session(session)
    return JSONResponse({"session": session})


@app.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str) -> JSONResponse:
    async with history_lock:
        path = _session_path(session_id)
        if not path.exists():
            raise HTTPException(status_code=404, detail="Session not found.")
        path.unlink()
    return JSONResponse({"ok": True})


@app.get("/api/history/search")
async def search_history(q: str = "", limit: int = 8) -> JSONResponse:
    terms = [term.lower() for term in re.findall(r"[\w'-]+", q) if len(term) > 1][:12]
    if not terms:
        return JSONResponse({"results": []})
    results: list[dict] = []
    async with history_lock:
        for path in SESSIONS_DIR.glob("*.json") if SESSIONS_DIR.exists() else []:
            session = _read_json(path, None)
            if not isinstance(session, dict):
                continue
            for index, message in enumerate(session.get("messages", [])):
                text = str(message.get("text", ""))
                lowered = text.lower()
                score = sum(lowered.count(term) for term in terms)
                if not score:
                    continue
                first = min((lowered.find(term) for term in terms if term in lowered), default=0)
                start, end = max(0, first - 140), min(len(text), first + 360)
                results.append({"session_id": session.get("id"), "title": session.get("title", "New conversation"),
                                "role": message.get("role", "assistant"), "text": text[start:end], "score": score,
                                "updated_at": session.get("updated_at", "")})
    results.sort(key=lambda item: (item["score"], item["updated_at"]), reverse=True)
    return JSONResponse({"results": results[:max(1, min(limit, 20))]})


@app.get("/api/personal-memory")
async def get_personal_memory() -> JSONResponse:
    async with history_lock:
        _migrate_legacy_memories()
        content = PERSONAL_MEMORY_PATH.read_text(encoding="utf-8") if PERSONAL_MEMORY_PATH.exists() else ""
    return JSONResponse({"content": content, "max_chars": PERSONAL_MEMORY_MAX_CHARS})


@app.put("/api/personal-memory")
async def put_personal_memory(req: ProfileUpdateRequest) -> JSONResponse:
    async with history_lock:
        _migrate_legacy_memories()
        content = _dedupe_profile_lines(req.content.strip())
        if len(content) > PERSONAL_MEMORY_MAX_CHARS:
            raise HTTPException(status_code=400, detail="Personal memory is too long; consolidate it first.")
        _atomic_write(PERSONAL_MEMORY_PATH, content + ("\n" if content else ""))
    return JSONResponse({"content": content, "max_chars": PERSONAL_MEMORY_MAX_CHARS})


@app.get("/")
def index() -> dict:
    """No browser UI remains; the native macOS app uses /api/* on this sidecar."""
    return {"ok": True, "service": "chatbot-sidecar", "ui": "removed"}
