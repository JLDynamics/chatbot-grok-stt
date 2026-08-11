"""
Tiny server for the chatbot demo.

The demo used to ship as a `sdk: static` Space, but the web-search tool needs a
search key the browser must NOT see. A static Space has no runtime process, so it
can't hold a secret the front-end uses. This server fixes that: it serves the
unchanged front-end AND exposes a same-origin `/api/search` proxy that holds the
Serper key server-side (see docs/adr/0001).

Everything lives in one container; the chatbot backend stays a separate,
load-balanced service the browser talks to over WebSocket as before. The load
balancer's address is a secret too (like the Serper key): the browser never sees
it. `/api/session` proxies the session handshake server-side so only the
per-session compute URL the LB hands back (which the browser must dial) is exposed.

On the deployed Space the server also meters conversation time by HF login tier
(anonymous / signed-in / PRO) — see `limiter.py` and `auth.py`. That whole feature
is off unless BOTH `LOAD_BALANCER_URL` and `SPACE_ID` are set, so it runs only on
the live Space, never locally (even with the LB exported for testing).

`SPEECH_TO_SPEECH_URL` overrides everything: when set, the LB logic above is
disabled entirely (no session proxy, no queue, no metering, no sign-in) and the
browser connects directly to that URL, shown read-only in Settings.

Endpoints:
  GET  /api/config           -> { search, lb, allowDirect, s2sUrl, rtc, iceServers, auth }
  GET  /api/me               -> login + tier + remaining budget (LB mode only)
  POST /api/search           -> { results, answer }  Google via Serper.dev
  POST /api/calls            -> proxies the WebRTC SDP offer to <s2s>/v1/realtime/calls
  POST /api/session          -> proxies <LB>/session: a grant, or a queue ticket
  GET  /api/queue/{id}       -> proxies <LB>/queue/{id}: position, or a grant on claim
  DELETE /api/queue/{id}     -> leave the queue (explicit "Leave queue" button)
  POST /api/queue/end        -> leave the queue (sendBeacon on teardown)
  POST /api/session/heartbeat-> extend the reservation; { expired }
  POST /api/session/end      -> reconcile + refund (sendBeacon on teardown)
  /*                         -> static files (index.html, main.js, ...)

When every compute slot is busy the load balancer hands back a queue ticket
instead of a grant; the browser polls /api/queue/{id} until it reaches the front
and a slot frees. Waiting reserves nothing — the daily budget is only reserved at
the moment a slot is actually claimed (a grant), never while queued.
"""

import asyncio
import ipaddress
import json
import logging
import os
import re
import socket
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

import auth
import httpx
import limiter
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

logger = logging.getLogger("s2s.search")
# uvicorn leaves the root logger at WARNING, which would swallow the per-search
# diagnostic below. That line is the only way to tell "no key reached the server"
# from "the provider rejected the key", so it has to be visible.
logger.setLevel(logging.INFO)

SERPER_KEY = os.environ.get("SERPER_API_KEY", "").strip()
# Tavily is an alternative search backend. Its free tier renews monthly, where
# Serper's free credits are a one-off allocation, so it suits a permanently free
# setup better. Either key works: the provider is chosen from the key's shape
# (Tavily keys start with "tvly-"), so a user can paste either one into the same
# Settings field with no extra UI.
TAVILY_KEY = os.environ.get("TAVILY_API_KEY", "").strip()
# Speech-to-speech load balancer URL. When set, the browser POSTs /api/session
# (which proxies <lb>/session here, server-side) and connects to the URL the LB
# returns (the original flow). The LB address itself is never sent to the browser.
# When empty, the user may instead set a direct s2s server URL in Settings and the
# browser connects to it straight (no load balancer).
LOAD_BALANCER_URL = os.environ.get("LOAD_BALANCER_URL", "").strip()
# Direct s2s server URL pinned by the deploy. Takes priority over the load
# balancer: when set, ALL LB logic is disabled (no /api/session proxy, no queue,
# no limiter, no sign-in) and the browser connects to this URL directly. Unlike
# the LB address it is NOT a secret — /api/config sends it to the client, which
# shows it read-only in Settings.
SPEECH_TO_SPEECH_URL = os.environ.get("SPEECH_TO_SPEECH_URL", "").strip()
if SPEECH_TO_SPEECH_URL:
    LOAD_BALANCER_URL = ""
# HF injects SPACE_ID ("owner/space") into every Space runtime; it's absent
# locally and on a plain `docker run`. We meter conversation time ONLY on the
# deployed Space — i.e. when BOTH the LB is configured AND we're on a Space.
# Off-Space (local dev, even with the LB exported) the app still proxies the LB,
# but nothing is metered: no budget, no reservations, no sign-in gating.
SPACE_ID = os.environ.get("SPACE_ID", "").strip()
LIMITER_ENABLED = bool(LOAD_BALANCER_URL) and bool(SPACE_ID)


def _parse_ice_servers(raw: str) -> list:
    """ICE servers for the browser's RTCPeerConnection, from RTC_ICE_SERVERS.

    Accepts a JSON list of RTCIceServer dicts (same format as the s2s
    server's SPEECH_TO_SPEECH_ICE_SERVERS, e.g.
    ``[{"urls": "turn:t.example.com", "username": "u", "credential": "c"}]``),
    a single such dict, or a plain comma-separated list of STUN/TURN URLs.
    Empty when unset — host candidates only, which is fine for local use."""
    raw = raw.strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return [data]
    except ValueError:
        pass
    return [{"urls": u.strip()} for u in raw.split(",") if u.strip()]


RTC_ICE_SERVERS = _parse_ice_servers(os.environ.get("RTC_ICE_SERVERS", ""))
DEFAULT_STARTUP_GREETING = (
    "Start the conversation now with a brief, spontaneous greeting in character. "
    "Keep it to one sentence, invite the user in naturally, and vary the wording each time."
)
# Exposed to the browser through /api/config. Set an empty value to disable the
# automatic greeting without changing the client bundle.
STARTUP_GREETING = os.environ.get("STARTUP_GREETING", DEFAULT_STARTUP_GREETING).strip()


def _webrtc_calls_url(s2s_url: str) -> str:
    """Derive the WebRTC handshake URL from the pinned realtime URL.

    ``ws://host:port/v1/realtime`` -> ``http://host:port/v1/realtime/calls``
    (ws->http, wss->https; a bare host gets the default /v1/realtime path,
    mirroring the client's buildDirectWsUrl normalisation)."""
    s = s2s_url.strip()
    if not s.startswith(("ws://", "wss://", "http://", "https://")):
        s = "http://" + s
    parts = urlsplit(s)
    scheme = {"ws": "http", "wss": "https"}.get(parts.scheme, parts.scheme)
    path = parts.path if parts.path not in ("", "/") else "/v1/realtime"
    return urlunsplit((scheme, parts.netloc, path.rstrip("/") + "/calls", parts.query, ""))


SERPER_URL = "https://google.serper.dev/search"
TAVILY_URL = "https://api.tavily.com/search"
TAVILY_KEY_PREFIX = "tvly-"
# Cap results so the tool output stays small enough to feed back to the model.
MAX_RESULTS = 5
HERE = os.path.dirname(os.path.abspath(__file__))
LB_USER_AGENT = "chatbot-demo"

app = FastAPI(title="s2s-demo")

# Wire HF OAuth before the app serves (no-op unless the OAuth env is present).
# Sign-in only matters when we're metering (prod Space), so gate it on that.
AUTH_ENABLED = LIMITER_ENABLED and auth.attach(app)


@app.on_event("startup")
async def _startup():
    """Stand up the usage DB and a periodic sweeper — metered (prod Space) only."""
    if not LIMITER_ENABLED:
        return
    limiter.init()
    asyncio.create_task(_sweeper())


async def _sweeper():
    while True:
        await asyncio.sleep(limiter.REAP_AFTER_SEC)
        try:
            await asyncio.to_thread(limiter.sweep)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("usage sweep failed: %r", exc)


class SearchRequest(BaseModel):
    query: str
    # Optional user-supplied key (fallback when the deploy has no server key).
    # Used for this request only; never stored.
    key: str | None = None


@app.get("/api/config")
def config():
    """Client bootstrap: whether web search is available, whether the deploy runs
    behind a load balancer (so the browser uses the /api/session proxy + limiter),
    whether HF sign-in is available, and whether the user may instead set a direct
    s2s server URL. The LB address itself is intentionally NOT included."""
    return {
        "search": bool(SERPER_KEY or TAVILY_KEY),
        "lb": bool(LOAD_BALANCER_URL),
        "allowDirect": not LOAD_BALANCER_URL,
        # Deploy-pinned direct s2s URL (empty when unset). Not a secret: the
        # browser dials it itself, and Settings shows it locked.
        "s2sUrl": SPEECH_TO_SPEECH_URL,
        # WebRTC transport availability: the /api/calls proxy only forwards to
        # the env-pinned URL (never a client-supplied one), so the toggle is
        # offered exactly when that URL exists.
        "rtc": bool(SPEECH_TO_SPEECH_URL),
        "iceServers": RTC_ICE_SERVERS,
        "startupGreeting": STARTUP_GREETING,
        "auth": AUTH_ENABLED,
    }


@app.get("/api/me")
async def me(request: Request):
    """Login state, tier, and remaining daily budget. Only meaningful in LB mode;
    sets the anonymous tracking cookie when first seen."""
    if not LIMITER_ENABLED:
        return {"enabled": False}
    view = auth.user_view(request)
    tier, keys, set_cookie = auth.resolve_identity(request)
    unlimited = limiter.budget_for(tier) is None
    rem = None if unlimited else await asyncio.to_thread(limiter.remaining, keys, tier)
    out = {
        "enabled": True,
        "auth": AUTH_ENABLED,
        **view,
        "remainingSec": rem,
        "limitSec": limiter.budget_for(tier),
        "loginUrl": auth.OAUTH_LOGIN_PATH if AUTH_ENABLED else None,
        "logoutUrl": auth.OAUTH_LOGOUT_PATH if AUTH_ENABLED else None,
    }
    resp = JSONResponse(out)
    if set_cookie:
        auth.set_anon_cookie(resp, set_cookie)
    return resp


def _clean_key(raw: Optional[str]) -> str:
    """Normalise a pasted API key.

    People paste what they were given, and what they are usually given is a
    shell line: `export TAVILY_API_KEY=tvly-...`. Pasted whole, that key starts
    with "expor", so provider detection sends it to the wrong service and the
    error says "Unauthorized" rather than "you pasted the wrong thing". Strip an
    `export`/`set` prefix, a `NAME=` assignment, and surrounding quotes.
    """
    key = (raw or "").strip()
    if not key:
        return ""
    key = re.sub(r"^\s*(?:export|set)\s+", "", key, flags=re.IGNORECASE)
    key = re.sub(r"^[A-Za-z_][A-Za-z0-9_]*\s*=\s*", "", key)
    key = key.strip().strip("'\"").strip()
    # A key never contains whitespace; if something else trailed along, keep the
    # first token rather than sending the whole line upstream.
    return key.split()[0] if key.split() else ""


@app.post("/api/search")
async def search(req: SearchRequest):
    """Proxy a Google search via Serper.dev. The key stays on the server unless
    the user brought their own (then theirs is used for this request only)."""
    query = (req.query or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="Empty query.")

    user_key = _clean_key(req.key)
    key = user_key or SERPER_KEY or TAVILY_KEY
    if not key:
        # No server key and the user didn't supply one — search is unavailable.
        # Logged, not just raised: silent 503s make this look like a broken tool
        # when the real cause is simply that no key reached the server.
        logger.warning(
            "search: no key available (browser sent none, no SERPER_API_KEY/TAVILY_API_KEY set)"
        )
        raise HTTPException(status_code=503, detail="Search is not configured.")

    # Pick the provider from the key itself, so either kind can be pasted into
    # the same Settings box without a provider dropdown.
    is_tavily = key.startswith(TAVILY_KEY_PREFIX)
    provider = "Tavily" if is_tavily else "Serper"
    # Never log the key itself -- only enough to tell a wrong-provider or
    # truncated-paste problem from a genuine auth failure.
    # WARNING, not INFO: uvicorn's root handler drops INFO records, and this one
    # line is what distinguishes "no key reached the server" from "the provider
    # rejected the key". One line per search is a fair price for that.
    logger.warning(
        "search: provider=%s source=%s key_len=%d prefix=%r query=%r",
        provider,
        "browser" if user_key else "server-env",
        len(key),
        key[:5],
        query[:60],
    )

    if is_tavily:
        url = TAVILY_URL
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        payload = {
            "query": query,
            "max_results": MAX_RESULTS,
            # Tavily can return a synthesised answer, matching Serper's answerBox
            # so the model gets the same shape either way.
            "include_answer": True,
        }
    else:
        url = SERPER_URL
        headers = {"X-API-KEY": key, "Content-Type": "application/json"}
        payload = {"q": query, "num": MAX_RESULTS}

    try:
        async with httpx.AsyncClient(timeout=12.0) as http:
            resp = await http.post(url, headers=headers, json=payload)
    except httpx.RequestError as exc:
        logger.warning("%s unreachable: %r", provider, exc)
        raise HTTPException(status_code=502, detail="Search provider unreachable.")

    if resp.status_code != 200:
        # The provider's error body carries the real reason (e.g. "Not enough
        # credits") and contains no key, so it's safe to log and relay.
        body = resp.text[:300]
        logger.warning("%s error %s: %s", provider, resp.status_code, body)
        msg = None
        try:
            payload_err = resp.json()
            msg = payload_err.get("message") or payload_err.get("detail") or payload_err.get("error")
            if isinstance(msg, dict):
                msg = msg.get("message")
        except Exception:
            pass
        detail = f"Search provider error ({resp.status_code})"
        if msg:
            detail += f": {msg}"
        raise HTTPException(status_code=502, detail=detail)

    data = resp.json()
    results = []
    if is_tavily:
        for item in (data.get("results") or [])[:MAX_RESULTS]:
            results.append(
                {
                    "title": item.get("title", ""),
                    # Tavily calls the excerpt "content"; trim it so tool output
                    # stays small enough to feed back to the model.
                    "snippet": (item.get("content") or "")[:300],
                    "url": item.get("url", ""),
                }
            )
        answer = data.get("answer") or None
    else:
        for item in (data.get("organic") or [])[:MAX_RESULTS]:
            results.append(
                {
                    "title": item.get("title", ""),
                    "snippet": item.get("snippet", ""),
                    "url": item.get("link", ""),
                }
            )

        # A direct answer when Google has one — saves the model a hop.
        box = data.get("answerBox") or {}
        answer = box.get("answer") or box.get("snippet") or None
        if not answer:
            kg = data.get("knowledgeGraph") or {}
            answer = kg.get("description") or None

    return JSONResponse({"query": query, "answer": answer, "results": results})


# ── Coding agent ────────────────────────────────────────────────────────────
#
# Hands a task to the pi coding agent (read/bash/edit/write) running on this
# machine. Jack chose the unrestricted scope deliberately, so there is no path
# allowlist here -- the trade is that everything pi is asked to do is logged to
# /tmp/s2s-web.log, and a single env var turns the whole tool off.
#
#   CODE_AGENT=off        disable entirely
#   CODE_AGENT_CWD=<dir>  where pi starts (default: $HOME)

PI_BIN = os.path.join(os.path.dirname(HERE), "node_modules", ".bin", "pi")
CODE_AGENT_ENABLED = os.environ.get("CODE_AGENT", "on").lower() not in ("off", "0", "false")
CODE_AGENT_CWD = os.path.expanduser(os.environ.get("CODE_AGENT_CWD", "~"))
CODE_AGENT_TIMEOUT_S = float(os.environ.get("CODE_AGENT_TIMEOUT", "300"))


class CodeRequest(BaseModel):
    task: str


@app.post("/api/code")
async def code_agent(req: CodeRequest):
    if not CODE_AGENT_ENABLED:
        raise HTTPException(status_code=503, detail="The coding agent is turned off.")
    task = (req.task or "").strip()
    if not task:
        raise HTTPException(status_code=400, detail="No task given.")
    if not os.path.exists(PI_BIN):
        raise HTTPException(
            status_code=503,
            detail="The pi coding agent is not installed. Run npm install in the project root.",
        )

    # Logged in full, deliberately: this is the one tool that changes the machine,
    # and a voice pipeline can mishear. The log is the audit trail.
    logger.warning("code_agent: cwd=%s task=%r", CODE_AGENT_CWD, task[:300])

    env = dict(os.environ)
    key = os.environ.get("OPENROUTER_API_KEY", "")
    if key:
        env["OPENROUTER_API_KEY"] = key

    try:
        proc = await asyncio.create_subprocess_exec(
            PI_BIN, "-p", task, "--provider", "openrouter",
            cwd=CODE_AGENT_CWD,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except OSError as exc:
        logger.warning("code_agent: could not start pi: %r", exc)
        raise HTTPException(status_code=502, detail="Could not start the coding agent.")

    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=CODE_AGENT_TIMEOUT_S)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        logger.warning("code_agent: timed out after %ss", CODE_AGENT_TIMEOUT_S)
        raise HTTPException(status_code=504, detail="The coding agent took too long and was stopped.")

    text = (out or b"").decode("utf-8", errors="replace").strip()
    logger.warning("code_agent: exit=%s output=%d chars", proc.returncode, len(text))
    # Spoken aloud, so a wall of build output helps nobody.
    if len(text) > 4000:
        text = text[:4000] + "\n[output truncated]"
    return JSONResponse({"ok": proc.returncode == 0, "exit_code": proc.returncode, "output": text})


# ── Desktop reading ─────────────────────────────────────────────────────────
#
# Reads the on-screen UI as structured text via desktop-harness, which uses the
# macOS accessibility tree. Far cheaper and more accurate than a screenshot for
# anything text-shaped: real labels and values instead of a JPEG to squint at.
#
# READ ONLY on purpose. desktop-harness can also click, type and drag; none of
# that is exposed here. A voice pipeline mishears ("DeepSeek" for OpenRouter,
# in this project's own history), and a misheard click is not undoable. Reading
# is safe to get wrong. If you want the acting half, the coding agent already
# has a shell and can call the CLI directly.
#
#   DESKTOP_READ=off   disable entirely
DESKTOP_HARNESS_BIN = os.path.expanduser("~/.local/bin/desktop-harness")
DESKTOP_READ_ENABLED = os.environ.get("DESKTOP_READ", "on").lower() not in ("off", "0", "false")
DESKTOP_READ_TIMEOUT_S = 30.0

# Windows whose UI text should never be dumped. desktop-harness blocks password
# managers itself, but its blocklist is per-app: a login form in a browser tab
# is just Safari, and its fields come back in the tree as plain text.
# Matched against the window TITLE and app name only -- never the page body.
# Scanning the body was wrong: "Forgot password?" appears somewhere on most of
# the web, so every read of a normal page was refused. What matters is whether
# the window *is* a credential screen, not whether it mentions one.
_LOGIN_HINTS = (
    "sign in", "sign-in", "log in", "log-in", "login",
    "password", "passcode", "two-factor", "2fa", "authenticator",
    "verify your identity", "one-time code", "checkout", "payment",
)


def _looks_like_credential_window(payload: dict) -> Optional[str]:
    """Is this window itself a sign-in/payment screen?

    Deliberately narrow. A false positive here silently breaks ordinary reading
    (it did); a false negative is covered by desktop-harness' own per-app block
    on password managers.
    """
    info = payload.get("info") or {}
    scope_parts = []
    front = info.get("frontmost")
    if isinstance(front, dict):
        scope_parts += [str(front.get("app", "")), str(front.get("title", ""))]
    elif front:
        scope_parts.append(str(front))
    for w in (info.get("windows") or [])[:12]:
        scope_parts.append(str(w.get("title", "")))
    scope = " ".join(scope_parts).lower()
    return next((h for h in _LOGIN_HINTS if h in scope), None)


class DesktopReadRequest(BaseModel):
    app: Optional[str] = None
    full: bool = False


# Sweep a window top to bottom in one shot: read, jump a full screen, read
# again, until it stops yielding new text.
#
# Two things this fixes, both of which come from the loop having lived in the
# model rather than in code:
#   - the model narrated between scrolls, because each scroll was its own tool
#     call and it answered after every one. One call means one answer.
#   - it stopped early, because a screenful of text looks like a whole article
#     from the inside. Code does not form that opinion.
#
# The jump is sized from the window height rather than a fixed line count: a
# wheel "line" is ~16px, so a 900px window is ~56 lines. 85% of a screen keeps
# a sliver of overlap so nothing falls between pages; duplicates are dropped.
_READ_FULL_SCRIPT = """
import json
from desktop_harness.helpers import labels, screen_info, scroll, wait

app = {app!r}
info = screen_info(app)

# Height of the window being read, falling back to a conservative screenful.
height = 800.0
for w in (info.get("windows") or []):
    if not app or app.lower() in (w.get("app") or "").lower():
        height = float(w.get("height") or height)
        break
page_lines = max(12, int(height / 16.0 * 0.85))

seen, ordered, dry, rounds = set(), [], 0, 0
for i in range({max_rounds}):
    rounds = i + 1
    fresh = 0
    for line in labels(app, limit=250):
        key = line.strip()
        if key and key not in seen:
            seen.add(key)
            ordered.append(key)
            fresh += 1
    # Two barren rounds means the bottom; one can just be a slow render.
    dry = dry + 1 if fresh == 0 else 0
    if dry >= 2:
        break
    scroll(dy=-page_lines)
    wait(0.35)

print(json.dumps({{
    "info": info,
    "labels": ordered,
    "rounds": rounds,
    "page_lines": page_lines,
}}))
"""


@app.post("/api/desktop/read")
async def desktop_read(req: DesktopReadRequest):
    if not DESKTOP_READ_ENABLED:
        raise HTTPException(status_code=503, detail="Desktop reading is turned off.")
    if not os.path.exists(DESKTOP_HARNESS_BIN):
        raise HTTPException(
            status_code=503,
            detail="desktop-harness is not installed. Run ./install.sh in ~/Documents/desktop-harness.",
        )

    target = (req.app or "").strip() or None

    if req.full:
        # 40 full-screen jumps covers a very long thread; each round is a read
        # plus a 0.35s settle, so the ceiling is well inside the timeout.
        script = _READ_FULL_SCRIPT.format(app=target, max_rounds=40)
        timeout = 180.0
    else:
        script = (
            "import json\n"
            "from desktop_harness.helpers import labels, screen_info\n"
            f"app = {target!r}\n"
            "info = screen_info(app)\n"
            "print(json.dumps({'info': info, 'labels': labels(app, limit=200)}))\n"
        )
        timeout = DESKTOP_READ_TIMEOUT_S

    logger.warning("desktop_read: app=%r full=%s", target or "(frontmost)", req.full)
    try:
        proc = await asyncio.create_subprocess_exec(
            DESKTOP_HARNESS_BIN, "-c", script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill(); await proc.wait()
        raise HTTPException(status_code=504, detail="Reading the screen took too long.")
    except OSError as exc:
        logger.warning("desktop_read: could not start: %r", exc)
        raise HTTPException(status_code=502, detail="Could not run desktop-harness.")

    text = (out or b"").decode("utf-8", errors="replace").strip()
    if proc.returncode != 0:
        # Most often: Accessibility permission not granted yet.
        detail = text[-300:] or "desktop-harness failed."
        if "accessibilit" in detail.lower() or "not trusted" in detail.lower():
            detail = "macOS Accessibility permission is not granted for the terminal running this."
        raise HTTPException(status_code=502, detail=detail)

    try:
        payload = json.loads(text.splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        raise HTTPException(status_code=502, detail="Could not read that window.")

    hit = _looks_like_credential_window(payload)
    if hit:
        logger.warning("desktop_read: refused, window looks like a login (%r)", hit)
        raise HTTPException(
            status_code=451,
            detail=(
                "That window looks like a sign-in or payment screen, so it was not read. "
                "Close it or switch windows and ask again."
            ),
        )

    return JSONResponse(payload)


# ── Desktop control ─────────────────────────────────────────────────────────
#
# The acting half: click, type, key, scroll, drag. Jack asked for the complete
# harness after being told the risk, so this is the full set.
#
# Two things are kept from the read-only version because they cost nothing:
# the login-screen guard still applies (a click into a sign-in form is worse
# than reading one), and every action is logged before it runs.
#
#   DESKTOP_CONTROL=off   disable the acting half, keep reading

DESKTOP_CONTROL_ENABLED = os.environ.get("DESKTOP_CONTROL", "on").lower() not in ("off", "0", "false")

# action -> (python expression template, needs a frontmost-window safety read)
_ACTIONS = {
    "click":  "click_text({text!r}, app)",
    "type":   "type_text({text!r})",
    "key":    "key({text!r})",
    "hotkey": "hotkey(*{keys!r})",
    "scroll": "scroll(dy={dy!r})",
    "drag":   "drag({x1!r}, {y1!r}, {x2!r}, {y2!r})",
}


class DesktopActRequest(BaseModel):
    action: str
    text: Optional[str] = None
    app: Optional[str] = None
    amount: Optional[int] = None
    coords: Optional[list[float]] = None


async def _run_harness(script: str, timeout: float) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        DESKTOP_HARNESS_BIN, "-c", script,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill(); await proc.wait()
        raise HTTPException(status_code=504, detail="That action took too long.")
    return proc.returncode, (out or b"").decode("utf-8", errors="replace").strip()


async def _frontmost_looks_like_login() -> Optional[str]:
    """Reuse the read path's guard before acting on a window."""
    script = (
        "import json\n"
        "from desktop_harness.helpers import labels, screen_info\n"
        "print(json.dumps({'info': screen_info(None), 'labels': labels(None, limit=60)}))\n"
    )
    try:
        code, text = await _run_harness(script, 20.0)
    except HTTPException:
        return None          # if the probe itself fails, don't block on it
    if code != 0 or not text:
        return None
    # Same narrowing as the read path: judge the window, not the page body.
    try:
        payload = json.loads(text.splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return None
    return _looks_like_credential_window(payload)


@app.post("/api/desktop/act")
async def desktop_act(req: DesktopActRequest):
    if not DESKTOP_CONTROL_ENABLED:
        raise HTTPException(status_code=503, detail="Desktop control is turned off.")
    if not os.path.exists(DESKTOP_HARNESS_BIN):
        raise HTTPException(status_code=503, detail="desktop-harness is not installed.")

    action = (req.action or "").strip().lower()
    if action not in _ACTIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown action {action!r}. Use one of: {', '.join(_ACTIONS)}.",
        )

    text = req.text or ""
    app_name = (req.app or "").strip() or None

    # Typing and clicking into a credential form is the failure that actually
    # costs something, so the guard runs before those.
    if action in ("click", "type", "key", "hotkey"):
        hit = await _frontmost_looks_like_login()
        if hit:
            logger.warning("desktop_act: refused %s, frontmost looks like a login (%r)", action, hit)
            raise HTTPException(
                status_code=451,
                detail=(
                    "The window in front looks like a sign-in or payment screen, so nothing "
                    "was clicked or typed. Switch windows and ask again."
                ),
            )

    if action == "scroll":
        # dy is in wheel *lines*, so the old default of 5 moved about two
        # paragraphs -- which is why scrolling an article appeared to do nothing.
        # Negative dy scrolls down in desktop-harness' convention.
        amount = req.amount if isinstance(req.amount, int) and req.amount else 15
        expr = _ACTIONS["scroll"].format(dy=-abs(amount) if amount >= 0 else abs(amount))
    elif action == "drag":
        c = req.coords or []
        if len(c) != 4:
            raise HTTPException(status_code=400, detail="Drag needs coords [x1, y1, x2, y2].")
        expr = _ACTIONS["drag"].format(x1=c[0], y1=c[1], x2=c[2], y2=c[3])
    elif action == "hotkey":
        keys = [k.strip().lower() for k in text.replace("+", " ").split() if k.strip()]
        if not keys:
            raise HTTPException(status_code=400, detail="Hotkey needs keys, e.g. 'cmd s'.")
        expr = _ACTIONS["hotkey"].format(keys=keys)
    else:
        if not text:
            raise HTTPException(status_code=400, detail=f"{action} needs text.")
        expr = _ACTIONS[action].format(text=text)

    logger.warning("desktop_act: %s app=%r arg=%r", action, app_name, text[:120])

    script = (
        "import json\n"
        "from desktop_harness.helpers import click_text, type_text, key, hotkey, scroll, drag\n"
        f"app = {app_name!r}\n"
        f"res = {expr}\n"
        "print(json.dumps({'ok': True, 'result': res if isinstance(res, (dict, list, str, int, float, bool, type(None))) else str(res)}))\n"
    )
    code, out = await _run_harness(script, 45.0)
    if code != 0:
        detail = out[-300:] or "The action failed."
        if "accessibilit" in detail.lower() or "not trusted" in detail.lower():
            detail = "macOS Accessibility permission is not granted."
        raise HTTPException(status_code=502, detail=detail)

    return JSONResponse({"ok": True, "action": action, "output": out[-500:]})


# ── Web fetch ───────────────────────────────────────────────────────────────
#
# web_search returns titles and snippets, never page content, so the model can
# know a URL exists and still be unable to read it. This fetches one page and
# reduces it to plain text the model can actually reason over.

FETCH_MAX_BYTES = 2_000_000     # stop pulling a page that is clearly not an article
FETCH_MAX_CHARS = 20_000        # cap what goes into the model's context
FETCH_TIMEOUT_S = 15.0


class _TextExtractor(HTMLParser):
    """Strip a page to readable text.

    No bs4/lxml: this needs to run in the demo's own tiny dependency set. Good
    enough for READMEs, docs and articles, which is what gets asked about.
    """

    SKIP = {"script", "style", "noscript", "svg", "head", "nav", "footer", "form"}
    BREAK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article"}

    MAIN = {"main", "article"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._parts: list[str] = []
        # Collected separately: if the page marks its content region, everything
        # outside it is navigation and sign-in chrome. GitHub spends ~2000
        # characters on that before the README starts.
        self._main_parts: list[str] = []
        self._main_depth = 0
        self.title: str | None = None
        self._in_title = False

    def _sink(self) -> list[str]:
        return self._main_parts if self._main_depth else self._parts

    def handle_starttag(self, tag, attrs):
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

    def handle_endtag(self, tag):
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

    def handle_data(self, data):
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
        raw = re.sub(r"\n\s*\n\s*\n+", "\n\n", raw)
        return raw.strip()

    def text(self) -> str:
        main = self._clean(self._main_parts)
        # Only trust <main> if it actually holds the substance; some pages wrap
        # a nav bar in <main> and put the article outside it.
        if len(main) >= 200:
            return main
        return self._clean(self._parts)


def _is_public_url(url: str) -> tuple[bool, str]:
    """Reject anything that is not a public http(s) address.

    This server runs on Jack's machine, so an unrestricted fetcher would happily
    read localhost admin pages, the speech server itself, or LAN devices on
    behalf of whatever a web page told the model to do.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return False, "Only http and https URLs can be fetched."
    host = parts.hostname
    if not host:
        return False, "That URL has no host."
    if host.rstrip(".").lower() in ("localhost", "localhost.localdomain"):
        return False, "Refusing to fetch a local address."
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False, f"Could not resolve {host}."
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_reserved:
            return False, "Refusing to fetch a private or loopback address."
    return True, ""


class FetchRequest(BaseModel):
    url: str


@app.post("/api/fetch")
async def fetch_page(req: FetchRequest):
    url = (req.url or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="No URL given.")
    if "://" not in url:
        url = "https://" + url

    ok, why = _is_public_url(url)
    if not ok:
        logger.warning("fetch: refused %r (%s)", url[:120], why)
        raise HTTPException(status_code=400, detail=why)

    logger.warning("fetch: %s", url[:200])
    try:
        async with httpx.AsyncClient(
            timeout=FETCH_TIMEOUT_S,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; chatbot/1.0)"},
        ) as http:
            resp = await http.get(url)
    except httpx.RequestError as exc:
        logger.warning("fetch: unreachable %r", exc)
        raise HTTPException(status_code=502, detail="Could not reach that page.")

    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"That page returned {resp.status_code}.")

    ctype = resp.headers.get("content-type", "")
    body = resp.content[:FETCH_MAX_BYTES]

    if "html" in ctype:
        parser = _TextExtractor()
        try:
            parser.feed(body.decode(resp.encoding or "utf-8", errors="replace"))
        except Exception:
            raise HTTPException(status_code=502, detail="Could not parse that page.")
        title, text = parser.title, parser.text()
    elif "text/" in ctype or "json" in ctype or "xml" in ctype:
        title, text = None, body.decode(resp.encoding or "utf-8", errors="replace").strip()
    else:
        raise HTTPException(status_code=415, detail=f"That is not a readable page ({ctype or 'unknown type'}).")

    truncated = len(text) > FETCH_MAX_CHARS
    if truncated:
        text = text[:FETCH_MAX_CHARS]

    return JSONResponse({
        "url": str(resp.url),
        "title": title,
        "text": text,
        "truncated": truncated,
    })


# ── Memories ────────────────────────────────────────────────────────────────
#
# Long-term memory for the voice agent. The model calls the client-side
# `remember`/`forget` tools; the client POSTs here; every new session loads the
# stored facts into its instructions. Deliberately a flat JSON file, not a
# vector store: one user's durable facts number in the dozens, where "search"
# is just "read them all into the prompt".

MEMORIES_PATH = os.path.expanduser(
    os.environ.get("S2S_MEMORIES_PATH", "~/.chatbot/memories.json")
)
_memories_lock = asyncio.Lock()


def _load_memories() -> list[dict]:
    try:
        with open(MEMORIES_PATH) as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _save_memories(items: list[dict]) -> None:
    os.makedirs(os.path.dirname(MEMORIES_PATH), exist_ok=True)
    tmp = MEMORIES_PATH + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(items, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, MEMORIES_PATH)


class MemoryRequest(BaseModel):
    text: str


@app.get("/api/memories")
async def list_memories():
    async with _memories_lock:
        return JSONResponse({"memories": _load_memories()})


@app.post("/api/memories")
async def add_memory(req: MemoryRequest):
    text = (req.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Empty memory.")
    if len(text) > 500:
        text = text[:500]
    async with _memories_lock:
        items = _load_memories()
        # Exact-duplicate guard so "remember X" twice doesn't double-store.
        for item in items:
            if item.get("text", "").strip().lower() == text.lower():
                return JSONResponse({"memory": item, "duplicate": True})
        record = {
            "id": max((int(i.get("id", 0)) for i in items), default=0) + 1,
            "text": text,
            "created": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        }
        items.append(record)
        _save_memories(items)
    return JSONResponse({"memory": record, "duplicate": False})


@app.delete("/api/memories/{memory_id}")
async def delete_memory(memory_id: int):
    async with _memories_lock:
        items = _load_memories()
        kept = [i for i in items if int(i.get("id", -1)) != memory_id]
        if len(kept) == len(items):
            raise HTTPException(status_code=404, detail="No such memory.")
        _save_memories(kept)
    return JSONResponse({"ok": True})


class ForgetRequest(BaseModel):
    text: str


@app.post("/api/memories/forget")
async def forget_memory(req: ForgetRequest):
    """Voice-friendly delete: match by content, since the model speaks in
    facts ("forget my sister's birthday"), not in row ids."""
    needle = (req.text or "").strip().lower()
    if not needle:
        raise HTTPException(status_code=400, detail="Empty forget request.")
    async with _memories_lock:
        items = _load_memories()
        matches = [
            i for i in items
            if needle in i.get("text", "").lower() or i.get("text", "").lower() in needle
        ]
        if not matches:
            return JSONResponse({"forgotten": [], "remaining": len(items)})
        kept = [i for i in items if i not in matches]
        _save_memories(kept)
    return JSONResponse({"forgotten": [m["text"] for m in matches], "remaining": len(kept)})


@app.post("/api/calls")
async def calls(request: Request):
    """Proxy the WebRTC SDP handshake to the pinned s2s server.

    The browser can't POST /v1/realtime/calls cross-origin (the s2s server has
    no CORS middleware, and an application/sdp POST is preflighted), so it
    posts the offer here and we forward it server-side. Only the signaling hop
    goes through this proxy — the negotiated audio/data-channel media flows
    directly between the browser and the s2s server.

    Deliberately forwards ONLY to SPEECH_TO_SPEECH_URL: honouring a
    client-supplied target would make this an open proxy (SSRF). No env pin,
    no WebRTC — the client keeps such setups on the WebSocket transport."""
    if not SPEECH_TO_SPEECH_URL:
        raise HTTPException(status_code=404, detail="Not found.")

    offer = await request.body()
    url = _webrtc_calls_url(SPEECH_TO_SPEECH_URL)
    try:
        # Generous timeout: the s2s server waits for its own ICE gathering
        # (up to ~5 s) before returning the answer.
        async with httpx.AsyncClient(timeout=15.0) as http:
            resp = await http.post(url, headers={"Content-Type": "application/sdp"}, content=offer)
    except httpx.RequestError as exc:
        logger.warning("s2s calls endpoint unreachable: %r", exc)
        raise HTTPException(status_code=502, detail="Speech service unreachable.")

    # Relay the answer (or the error body) as-is; keep the Location header the
    # s2s server sets on success (the call id, per the OpenAI GA contract).
    headers = {}
    if "location" in resp.headers:
        headers["Location"] = resp.headers["location"]
    return Response(
        content=resp.content,
        status_code=resp.status_code,
        media_type=resp.headers.get("content-type", "application/sdp"),
        headers=headers,
    )


@app.post("/api/session")
async def session(request: Request):
    """Proxy the session handshake to the load balancer, keeping its URL secret,
    and meter conversation time by tier.

    The browser POSTs here (same-origin); we resolve the caller's tier, refuse if
    today's budget is already spent (402), otherwise POST <LOAD_BALANCER_URL>/session
    and relay the JSON back. The LB body carries a per-session `connect_url`
    (compute host + short-lived token) the browser must dial directly — that one
    URL is unavoidably exposed, but the stable load-balancer address is not. On a
    successful grant we reserve the first time chunk against the day's budget."""
    if not LOAD_BALANCER_URL:
        # No LB configured — this deploy is direct-mode only; the browser should
        # never call this. 404 so it's indistinguishable from a missing route.
        raise HTTPException(status_code=404, detail="Not found.")

    login_reason = auth.oauth_login_required_reason(request)
    if login_reason:
        return _login_required_response(login_reason)

    tier, keys, set_cookie = auth.resolve_identity(request)
    # Metering runs only on the deployed Space; off-Space the LB still proxies but
    # nothing is tracked. Within metering, unlimited tiers (pro, org) aren't either.
    tracked = LIMITER_ENABLED and limiter.budget_for(tier) is not None

    # Refuse before troubling the LB if the day's budget is already gone. Done
    # here (at enqueue) so we never put a user who can't talk into the queue.
    if tracked:
        rem = await asyncio.to_thread(limiter.remaining, keys, tier)
        if rem is not None and rem <= 0:
            resp = JSONResponse(
                {"tier": tier, "reason": "limit", "remainingSec": 0}, status_code=402
            )
            if set_cookie:
                auth.set_anon_cookie(resp, set_cookie)
            return resp

    url = f"{LOAD_BALANCER_URL.rstrip('/')}/session"
    try:
        async with httpx.AsyncClient(timeout=15.0) as http:
            lb = await http.post(
                url,
                headers=_load_balancer_headers(request),
                content="{}",
            )
    except httpx.RequestError as exc:
        logger.warning("Load balancer unreachable: %r", exc)
        raise HTTPException(status_code=502, detail="Speech service unreachable.")

    # The queue is full: the LB replies 503 {state:"at_capacity"}. Relay it as-is
    # so the client shows a soft "try again shortly", not a hard error.
    if lb.status_code == 503:
        body = _safe_json(lb)
        if body.get("state") == "at_capacity":
            resp = JSONResponse({"state": "at_capacity"}, status_code=503)
            if set_cookie:
                auth.set_anon_cookie(resp, set_cookie)
            return resp

    if lb.status_code == 401:
        body = _safe_json(lb)
        reason = body.get("reason", "login_required")
        if reason == "token_invalid":
            session = getattr(request, "scope", {}).get("session")
            if isinstance(session, dict):
                session.pop("oauth_info", None)
        logger.info("Session authentication rejected: %s", reason)
        return _login_required_response(reason, set_cookie)

    if lb.status_code != 200:
        # The LB's error body may name the reason (e.g. capacity); it carries no
        # secret, so relay a trimmed copy.
        logger.warning("Session handshake failed %s: %s", lb.status_code, lb.text[:300])
        raise HTTPException(status_code=502, detail=f"Session handshake failed ({lb.status_code}).")

    data = lb.json()

    # Busy pool: the LB queued us. Relay the ticket untouched — crucially with NO
    # reservation, so waiting in line never costs the day's budget.
    if data.get("state") == "queued":
        data["tier"] = tier
        resp = JSONResponse(data)
        if set_cookie:
            auth.set_anon_cookie(resp, set_cookie)
        return resp

    # A slot was free: reserve the first chunk now and return the grant.
    return await _finalize_grant(data, keys, tier, tracked, set_cookie)


def _login_required_response(reason: str, set_cookie=None) -> JSONResponse:
    """Actionable 401 understood by the browser's login-required flow."""
    resp = JSONResponse(
        {
            "reason": reason,
            "loginUrl": auth.OAUTH_LOGIN_PATH if AUTH_ENABLED else None,
        },
        status_code=401,
    )
    if set_cookie:
        auth.set_anon_cookie(resp, set_cookie)
    return resp


def _load_balancer_headers(request: Request) -> dict[str, str]:
    """Headers for the server-to-server session allocation request.

    The dedicated authorization header matches the Reachy Mini client and lets
    the load balancer validate and attribute an optional HF user token without
    exposing it to browser JavaScript. Anonymous visitors send no credential.
    """
    headers = {
        "Content-Type": "application/json",
        "User-Agent": LB_USER_AGENT,
    }
    token = auth.current_access_token(request)
    if token:
        headers["X-Reachy-Mini-Authorization"] = f"Bearer {token}"
    return headers


@app.get("/api/queue/{queue_id}")
async def queue_status(queue_id: str, request: Request):
    """Poll a waiting ticket: relay the position, or — when the head of the line
    claims a freed slot — reserve the budget now and return the grant. Re-checks the
    daily budget at claim, since a multi-minute wait could have spent it elsewhere."""
    if not LOAD_BALANCER_URL:
        raise HTTPException(status_code=404, detail="Not found.")

    login_reason = auth.oauth_login_required_reason(request)
    if login_reason:
        return _login_required_response(login_reason)

    tier, keys, set_cookie = auth.resolve_identity(request)
    tracked = LIMITER_ENABLED and limiter.budget_for(tier) is not None

    url = f"{LOAD_BALANCER_URL.rstrip('/')}/queue/{queue_id}"
    try:
        async with httpx.AsyncClient(timeout=15.0) as http:
            lb = await http.get(url)
    except httpx.RequestError as exc:
        logger.warning("Load balancer unreachable: %r", exc)
        raise HTTPException(status_code=502, detail="Speech service unreachable.")

    if lb.status_code == 404:
        # Ticket unknown/expired (reaped after we stopped polling). Tell the client
        # to start over rather than spin.
        resp = JSONResponse({"state": "expired"}, status_code=404)
        if set_cookie:
            auth.set_anon_cookie(resp, set_cookie)
        return resp

    if lb.status_code != 200:
        logger.warning("Queue poll failed %s: %s", lb.status_code, lb.text[:300])
        raise HTTPException(status_code=502, detail=f"Queue poll failed ({lb.status_code}).")

    data = lb.json()

    if data.get("state") == "queued":
        data["tier"] = tier
        resp = JSONResponse(data)
        if set_cookie:
            auth.set_anon_cookie(resp, set_cookie)
        return resp

    # Claimed a slot. Re-check the budget: it may have been spent in another tab
    # during the wait. If so, refuse — the just-claimed slot is now a pending
    # session on the LB and its pending-timeout reaper reclaims it shortly.
    if tracked:
        rem = await asyncio.to_thread(limiter.remaining, keys, tier)
        if rem is not None and rem <= 0:
            resp = JSONResponse(
                {"tier": tier, "reason": "limit", "remainingSec": 0}, status_code=402
            )
            if set_cookie:
                auth.set_anon_cookie(resp, set_cookie)
            return resp

    return await _finalize_grant(data, keys, tier, tracked, set_cookie)


@app.delete("/api/queue/{queue_id}")
async def queue_leave(queue_id: str):
    """Leave the queue from the explicit 'Leave queue' button (a real fetch)."""
    if not LOAD_BALANCER_URL:
        raise HTTPException(status_code=404, detail="Not found.")
    await _lb_leave(queue_id)
    return {"ok": True}


@app.post("/api/queue/end")
async def queue_end(request: Request):
    """Leave the queue on teardown/tab-close (navigator.sendBeacon, which can only
    POST). Body: { queueId }. Best-effort; the LB reaps the ticket on TTL anyway."""
    if not LOAD_BALANCER_URL:
        raise HTTPException(status_code=404, detail="Not found.")
    qid = await _queue_id(request)
    if qid:
        await _lb_leave(qid)
    return {"ok": True}


async def _finalize_grant(data, keys, tier, tracked, set_cookie):
    """Shared grant tail (fast path or queue claim): reserve the first chunk, attach
    the metering fields the client needs, and set the anon cookie."""
    remaining = None
    if tracked and data.get("session_id"):
        await asyncio.to_thread(limiter.begin, data["session_id"], keys, tier)
        remaining = await asyncio.to_thread(limiter.remaining, keys, tier)

    data.update({
        "tier": tier,
        "limited": tracked,
        "remainingSec": remaining,
        "heartbeatSec": limiter.HEARTBEAT_SEC,
    })
    resp = JSONResponse(data)
    if set_cookie:
        auth.set_anon_cookie(resp, set_cookie)
    return resp


async def _lb_leave(queue_id: str) -> None:
    """Best-effort: tell the LB to drop a waiting ticket."""
    url = f"{LOAD_BALANCER_URL.rstrip('/')}/queue/{queue_id}"
    try:
        async with httpx.AsyncClient(timeout=5.0) as http:
            await http.delete(url)
    except httpx.RequestError as exc:
        logger.warning("Queue leave failed: %r", exc)


def _safe_json(response) -> dict:
    try:
        body = response.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


async def _queue_id(request: Request) -> str:
    """Pull `queueId` from a JSON body, tolerating sendBeacon's blob posts."""
    try:
        data = await request.json()
    except Exception:
        return ""
    return (data or {}).get("queueId", "") if isinstance(data, dict) else ""


async def _session_id(request: Request) -> str:
    """Pull `sessionId` from a JSON body, tolerating sendBeacon's blob posts."""
    try:
        data = await request.json()
    except Exception:
        return ""
    return (data or {}).get("sessionId", "") if isinstance(data, dict) else ""


@app.post("/api/session/heartbeat")
async def session_heartbeat(request: Request):
    """Extend the live reservation one chunk at a time. `expired` once the day's
    budget is spent — the client then tears down."""
    if not LIMITER_ENABLED:
        raise HTTPException(status_code=404, detail="Not found.")
    sid = await _session_id(request)
    alive = bool(sid) and await asyncio.to_thread(limiter.heartbeat, sid)
    return {"expired": not alive}


@app.post("/api/session/end")
async def session_end(request: Request):
    """Clean teardown: reconcile to real elapsed time and refund the unused
    chunk. Sent via navigator.sendBeacon, so it must succeed without a response."""
    if not LIMITER_ENABLED:
        raise HTTPException(status_code=404, detail="Not found.")
    sid = await _session_id(request)
    if sid:
        await asyncio.to_thread(limiter.end, sid)
    return {"ok": True}


# Static front-end. Registered last so the /api routes win. `html=True` serves
# index.html at "/". The repo is public anyway, so serving the dir is fine.
app.mount("/", StaticFiles(directory=HERE, html=True), name="static")
