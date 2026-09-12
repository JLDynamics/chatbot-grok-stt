"""Desktop-control concern: guarded access to the local desktop-harness binary."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import tempfile
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

logger = logging.getLogger("chatbot.sidecar")

router = APIRouter()


async def _while_connected(work, request: Request, timeout: float):
    stopped = asyncio.Event()

    async def disconnected():
        while not stopped.is_set():
            if await request.is_disconnected():
                return
            await asyncio.sleep(0.1)

    running = asyncio.ensure_future(work)
    watcher = asyncio.create_task(disconnected())
    try:
        done, _ = await asyncio.wait({running, watcher}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        if running in done:
            return await running
        if watcher in done:
            raise asyncio.CancelledError("The tool client disconnected")
        raise asyncio.TimeoutError
    finally:
        # Starlette's receive probe uses an AnyIO cancellation scope which can
        # consume a Task.cancel() arriving during that probe. The stop flag
        # ensures the watcher exits on its next iteration in that case too.
        stopped.set()
        for task in (running, watcher):
            if not task.done():
                task.cancel()
        await asyncio.gather(running, watcher, return_exceptions=True)


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
    except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
        if process.returncode is None:
            process.kill()
        await process.wait()
        if isinstance(exc, asyncio.CancelledError):
            raise
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
    if not isinstance(payload, dict):
        raise HTTPException(status_code=502, detail="Desktop Harness returned invalid context metadata.")
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


async def _screenshot_target_is_sensitive(app: str | None) -> str | None:
    """Block capturing a login/payment/password-manager window.

    Unlike click/type, a full-display screenshot must not treat a "Sign in"
    button somewhere in the accessibility tree as a hard block — that is how
    ordinary Chrome pages were denying every screenshot.
    """
    if app:
        lowered = app.lower()
        return next((hint for hint in LOGIN_HINTS if hint in lowered), None)
    try:
        front = await _desktop_frontmost_context()
    except HTTPException:
        return None
    return "login" if front.get("sensitive") else None


async def _capture_desktop_screenshot(app: str | None) -> dict[str, str]:
    script = (
        "import json\n"
        "import Quartz\n"
        "if not Quartz.CGPreflightScreenCaptureAccess():\n"
        "    raise PermissionError('Screen Recording permission is not enabled for the app running Chatbot')\n"
        "from desktop_harness.helpers import screenshot\n"
        f"path = screenshot(app={app!r})\n"
        # TCC denial yields a valid but near-uniform black PNG (not None), which
        # used to sail through size/PNG checks and reach the model as a "dark"
        # image. A legit dark-mode desktop still has bright text (high variance);
        # a denied capture has ~zero pixels above 0.25. Report the bright
        # fraction so the sidecar can return a 403 permission hint instead.
        "from AppKit import NSImage, NSBitmapImageRep\n"
        "img = NSImage.alloc().initWithContentsOfFile_(path)\n"
        "rep = NSBitmapImageRep.imageRepWithData_(img.TIFFRepresentation())\n"
        "w, h = int(rep.pixelsWide()), int(rep.pixelsHigh())\n"
        "sx, sy = max(1, w // 64), max(1, h // 64)\n"
        "bright, n = 0, 0\n"
        "for yy in range(0, h, sy):\n"
        "    for xx in range(0, w, sx):\n"
        "        c = rep.colorAtX_y_(xx, yy)\n"
        "        if c is None:\n"
        "            continue\n"
        "        c = c.colorUsingColorSpaceName_('NSCalibratedRGBColorSpace') or c\n"
        "        n += 1\n"
        "        if (c.redComponent() + c.greenComponent() + c.blueComponent()) / 3.0 > 0.25:\n"
        "            bright += 1\n"
        "frac = (bright / n) if n else 1.0\n"
        "print(json.dumps({'path': path, 'bright_frac': frac, 'samples': n}))\n"
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
                    "app running Chatbot (Voice when started from Voice), then quit and reopen it. "
                    "No screenshot was captured. Do not retry until permission is enabled."
                ),
            )
        raise HTTPException(status_code=502, detail=detail)
    try:
        payload = json.loads(output.splitlines()[-1])
        path = Path(payload["path"]).resolve(strict=True)
        bright_frac = float(payload.get("bright_frac", 1.0))
    except (json.JSONDecodeError, IndexError, KeyError, OSError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=502, detail="Desktop Harness returned an invalid screenshot path.") from exc
    if bright_frac < 0.005:
        raise HTTPException(
            status_code=403,
            detail=(
                "Screen capture returned a black image (permission denied). Grant Screen Recording "
                "permission to the app running Chatbot (Voice when started from Voice, else Terminal/Python), "
                "then quit and reopen it. No usable screenshot was captured. Do not retry until permission is enabled."
            ),
        )
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


@router.post("/api/desktop/act")
async def desktop_act(req: DesktopActRequest, request: Request) -> JSONResponse:
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
    if action in {
        "click",
        "type",
        "key",
        "hotkey",
        "scroll",
        "drag",
    } and await _screen_scope_looks_sensitive(sensitive_scope):
        raise HTTPException(status_code=451, detail="Desktop control is blocked on sign-in and payment windows.")
    if action == "screenshot" and await _screenshot_target_is_sensitive(app_name):
        raise HTTPException(status_code=451, detail="Desktop control is blocked on sign-in and payment windows.")
    if action == "screenshot":
        captured = await _while_connected(_capture_desktop_screenshot(app_name), request, 60.0)
        return JSONResponse({"ok": True, "action": action, **captured})
    text = req.text or ""
    preamble = ""
    if action == "scroll":
        raw = req.amount if req.amount else 5
        # dy unit is LINES: raw amounts like 5-7 crawl a few lines per round,
        # so long pages take forever and repeat-detection stops early on heavy
        # overlap. Scale to full strides (~half screen) and clamp for sanity.
        stride = max(1, min(abs(raw), 40)) * 6
        expression = ACTIONS[action].format(dy=-stride if raw >= 0 else stride)
        if not app_name:
            # The model often omits app. Without focus + pointer placement the
            # wheel events land wherever the cursor happens to be and scroll()
            # reports ok while nothing moves (proven by byte-identical captures
            # across repeated scrolls). Default to the frontmost app so the
            # preamble below still focuses it and centers the pointer.
            try:
                front = await _desktop_frontmost_context()
                candidate = str(front.get("app") or "").strip()[:120] or None
            except HTTPException:
                candidate = None
            if candidate:
                app_name = candidate
                if await _screen_scope_looks_sensitive(app_name):
                    raise HTTPException(
                        status_code=451, detail="Desktop control is blocked on sign-in and payment windows."
                    )
        if app_name:
            # macOS delivers scroll wheel events to the focused app, at the
            # pointer. Without both, scroll() silently no-ops: it reported
            # ok=true while the screen never moved, so a caller reading a long
            # page would loop forever on the same screenful. Focus the target
            # and put the pointer inside it first.
            preamble = (
                "open_app(app)\n"
                "wait(0.6)\n"
                "_frame = window_frame(app)\n"
                "if _frame:\n"
                "    move_to(_frame['x'] + _frame['w'] / 2, _frame['y'] + _frame['h'] / 2)\n"
            )
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
        "from desktop_harness.helpers import open_app, window_frame, move_to, wait\n"
        f"app = {app_name!r}\n"
        f"verify = {action in {'click', 'type', 'key', 'hotkey'}!r}\n"
        "before = set(labels(app, limit=60)) if verify else set()\n"
        f"{preamble}"
        f"result = {expression}\n"
        "if verify: wait_stable(0.35)\n"
        "changed = [x for x in labels(app, limit=60) if x and x not in before][:12] if verify else []\n"
        "print(json.dumps({'ok': result is not False, 'result': str(result), 'changed': changed, 'verified': bool(changed)}))\n"
    )
    code, output = await _while_connected(_run_harness(script, 45.0), request, 60.0)
    if code != 0:
        raise HTTPException(status_code=502, detail=output[-300:] or "Desktop action failed.")
    try:
        payload = json.loads(output.splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        raise HTTPException(status_code=502, detail="Desktop Harness returned an invalid result.")
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        raise HTTPException(status_code=502, detail="Desktop Harness did not complete that action.")
    return JSONResponse({"ok": True, "action": action, **payload})
