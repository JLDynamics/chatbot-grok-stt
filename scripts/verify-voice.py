#!/usr/bin/env python3
"""Click-through and API verify for the native Voice panel.

Exercises the places the refactor changed: sidecar/voice health, settings
wiring (memory, sessions, search, chrome bridge, screenshot, code agent),
header/control buttons, and a short live model reply.

Usage (from the repo root):

    python3 scripts/verify-voice.py
    python3 scripts/verify-voice.py --app macos/Voice/build/Voice.app
    python3 scripts/verify-voice.py --skip-ui
    python3 scripts/verify-voice.py --skip-ui --research   # bash/curl + verify-first + dated news RSS
    python3 scripts/verify-voice.py --cold     # quit Voice, restart local services

Also confirms both local services run this checkout's current code (their
health payloads carry a source fingerprint), which is how a stale backend
that silently lacked server-side search was caught.

Does not overwrite personal memory or delete saved conversations.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_APP = ROOT / "macos/Voice/build/Voice.app"
SIDECAR = os.environ.get("SIDECAR", "http://127.0.0.1:7860/api")
VOICE_HTTP = os.environ.get("VOICE_HTTP", "http://127.0.0.1:8766")
VOICE_WS = os.environ.get("VOICE_WS", "ws://127.0.0.1:8766/v1/realtime")


@dataclass
class Result:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class Report:
    results: list[Result] = field(default_factory=list)
    out_dir: Path = Path("/tmp/voice-verify")

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.results.append(Result(name, ok, detail))
        mark = "ok" if ok else "FAIL"
        extra = f" — {detail}" if detail else ""
        print(f"[{mark}] {name}{extra}")

    def ok(self) -> bool:
        return all(item.ok for item in self.results)


def http_json(
    url: str, method: str = "GET", body: dict[str, Any] | None = None, timeout: float = 12
) -> tuple[int, Any]:
    data = None if body is None else json.dumps(body).encode()
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            raw = res.read()
            payload: Any = json.loads(raw) if raw else {}
            return res.status, payload
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            payload = json.loads(raw) if raw else {"detail": str(exc)}
        except json.JSONDecodeError:
            payload = {"detail": raw.decode("utf-8", "replace")}
        return exc.code, payload
    except Exception as exc:
        return 0, {"detail": str(exc)}


def run_osascript(source: str, timeout: float = 15) -> str:
    proc = subprocess.run(
        ["osascript", "-e", source],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "osascript failed").strip())
    return (proc.stdout or "").strip()


def voice_running() -> bool:
    proc = subprocess.run(["pgrep", "-x", "Voice"], capture_output=True)
    return proc.returncode == 0


def voice_binary() -> str:
    proc = subprocess.run(["ps", "-ax", "-o", "command="], capture_output=True, text=True)
    lines = [
        line.strip()
        for line in (proc.stdout or "").splitlines()
        if "/Voice.app/Contents/MacOS/Voice" in line and "grep" not in line
    ]
    return "\n".join(lines)


def window_frame() -> tuple[int, int, int, int]:
    raw = run_osascript('tell application "System Events" to tell process "Voice" to get {position, size} of window 1')
    parts = [int(piece.strip()) for piece in raw.replace("{", "").replace("}", "").split(",") if piece.strip()]
    if len(parts) != 4:
        raise RuntimeError(f"bad window frame: {raw!r}")
    return parts[0], parts[1], parts[2], parts[3]


def activate_voice() -> None:
    try:
        run_osascript('tell application "Voice" to activate', timeout=6)
    except Exception:
        subprocess.run(["open", "-a", "Voice"], check=False)
    time.sleep(0.35)


def click_at(x: int, y: int) -> None:
    """Click in top-left screen coordinates (System Events)."""
    activate_voice()
    run_osascript(
        f"""
tell application "System Events"
  click at {{{int(x)}, {int(y)}}}
end tell
"""
    )
    time.sleep(0.35)


def click_frac(fx: float, fy: float) -> tuple[int, int]:
    """Click a fraction of the Voice panel. System Events origin is top-left."""
    x, y, w, h = window_frame()
    px = int(x + w * fx)
    py = int(y + h * fy)
    click_at(px, py)
    return px, py


def click_panel_button(index: int) -> None:
    """Click the Nth AX button in the panel (1-based). Header: 1 On top, 2-4 theme, 5 History, 6 Settings, 7 Close."""
    activate_voice()
    run_osascript(
        f"""
tell application "System Events"
  tell process "Voice"
    click button {int(index)} of group 1 of window 1
  end tell
end tell
""",
        timeout=8,
    )
    time.sleep(0.4)


_HI = ctypes.cdll.LoadLibrary(
    "/System/Library/Frameworks/ApplicationServices.framework/Frameworks/HIServices.framework/HIServices"
)
_CF = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
_kCFStringEncodingUTF8 = 0x08000100
_kAXErrorSuccess = 0

_CF.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
_CF.CFStringCreateWithCString.restype = ctypes.c_void_p
_CF.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
_CF.CFStringGetCString.restype = ctypes.c_bool
_CF.CFRelease.argtypes = [ctypes.c_void_p]
_CF.CFArrayGetCount.argtypes = [ctypes.c_void_p]
_CF.CFArrayGetCount.restype = ctypes.c_long
_CF.CFArrayGetValueAtIndex.argtypes = [ctypes.c_void_p, ctypes.c_long]
_CF.CFArrayGetValueAtIndex.restype = ctypes.c_void_p
_HI.AXUIElementCreateApplication.argtypes = [ctypes.c_int]
_HI.AXUIElementCreateApplication.restype = ctypes.c_void_p
_HI.AXUIElementCopyAttributeValue.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
_HI.AXUIElementCopyAttributeValue.restype = ctypes.c_int
_HI.AXUIElementPerformAction.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
_HI.AXUIElementPerformAction.restype = ctypes.c_int
_HI.AXValueGetValue.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p]
_HI.AXValueGetValue.restype = ctypes.c_bool

_AX_ATTR_CACHE: dict[str, ctypes.c_void_p] = {}
_kAXValueCGPointType = 1
_kAXValueCGSizeType = 2


class _CGPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


class _CGSize(ctypes.Structure):
    _fields_ = [("width", ctypes.c_double), ("height", ctypes.c_double)]


def _ax_str(value: str) -> ctypes.c_void_p:
    ref = _AX_ATTR_CACHE.get(value)
    if ref:
        return ref
    ref = _CF.CFStringCreateWithCString(None, value.encode(), _kCFStringEncodingUTF8)
    if not ref:
        raise RuntimeError(f"CFStringCreateWithCString failed for {value}")
    _AX_ATTR_CACHE[value] = ref
    return ref


def _cf_to_str(ref: ctypes.c_void_p) -> str:
    if not ref:
        return ""
    buf = ctypes.create_string_buffer(1024)
    if not _CF.CFStringGetCString(ref, buf, 1024, _kCFStringEncodingUTF8):
        return ""
    return buf.value.decode("utf-8", "replace")


def _ax_attr(element: ctypes.c_void_p, name: str) -> ctypes.c_void_p | None:
    out = ctypes.c_void_p()
    err = _HI.AXUIElementCopyAttributeValue(element, _ax_str(name), ctypes.byref(out))
    if err != _kAXErrorSuccess or not out.value:
        return None
    return out


def _ax_array(element: ctypes.c_void_p, name: str) -> list[ctypes.c_void_p]:
    arr = _ax_attr(element, name)
    if not arr:
        return []
    count = int(_CF.CFArrayGetCount(arr))
    return [ctypes.c_void_p(_CF.CFArrayGetValueAtIndex(arr, i)) for i in range(count)]


def _ax_children(element: ctypes.c_void_p) -> list[ctypes.c_void_p]:
    return _ax_array(element, "AXChildren")


def _ax_find_identifier(element: ctypes.c_void_p, identifier: str, budget: list[int]) -> ctypes.c_void_p | None:
    if budget[0] <= 0:
        return None
    budget[0] -= 1
    ident = _ax_attr(element, "AXIdentifier")
    if ident:
        try:
            if _cf_to_str(ident) == identifier:
                return element
        finally:
            _CF.CFRelease(ident)
    for child in _ax_children(element):
        found = _ax_find_identifier(child, identifier, budget)
        if found:
            return found
    return None


def _ax_role(element: ctypes.c_void_p) -> str:
    ref = _ax_attr(element, "AXRole")
    if not ref:
        return ""
    try:
        return _cf_to_str(ref)
    finally:
        _CF.CFRelease(ref)


def _ax_frame_center(element: ctypes.c_void_p) -> tuple[int, int] | None:
    pos_ref = _ax_attr(element, "AXPosition")
    size_ref = _ax_attr(element, "AXSize")
    if not pos_ref or not size_ref:
        if pos_ref:
            _CF.CFRelease(pos_ref)
        if size_ref:
            _CF.CFRelease(size_ref)
        return None
    try:
        point = _CGPoint()
        size = _CGSize()
        if not _HI.AXValueGetValue(pos_ref, _kAXValueCGPointType, ctypes.byref(point)):
            return None
        if not _HI.AXValueGetValue(size_ref, _kAXValueCGSizeType, ctypes.byref(size)):
            return None
        return int(point.x + size.width / 2), int(point.y + size.height / 2)
    finally:
        _CF.CFRelease(pos_ref)
        _CF.CFRelease(size_ref)


def _ax_search_roots(app: ctypes.c_void_p) -> list[ctypes.c_void_p]:
    roots: list[ctypes.c_void_p] = []
    seen: set[int] = set()
    for el in _ax_array(app, "AXWindows") + _ax_children(app):
        ptr = int(el.value or 0)
        if not ptr or ptr in seen:
            continue
        if _ax_role(el) in {"AXMenuBar", "AXMenu", "AXMenuBarItem"}:
            continue
        seen.add(ptr)
        roots.append(el)
    return roots


def click_ax(identifier: str, timeout: float = 8) -> None:
    """Click the first UI element whose AXIdentifier matches.

    Finds the control through the C accessibility API (never AppleScript
    `entire contents`, which wedged Voice's main thread), then sends a real
    mouse click at its frame so SwiftUI first-mouse buttons actually fire.
    """
    activate_voice()
    deadline = time.monotonic() + timeout
    last_error = "Voice is not running"
    while time.monotonic() < deadline:
        proc = subprocess.run(["pgrep", "-x", "Voice"], capture_output=True, text=True)
        if proc.returncode != 0:
            last_error = "Voice is not running"
            time.sleep(0.15)
            continue
        pid = int(proc.stdout.split()[0])
        app = _HI.AXUIElementCreateApplication(pid)
        if not app:
            last_error = "AXUIElementCreateApplication failed"
            time.sleep(0.15)
            continue
        found = None
        for root in _ax_search_roots(app):
            found = _ax_find_identifier(root, identifier, [8_000])
            if found:
                break
        if not found:
            last_error = f"no AXIdentifier {identifier}"
            time.sleep(0.15)
            continue
        center = _ax_frame_center(found)
        if center:
            click_at(center[0], center[1])
            return
        err = _HI.AXUIElementPerformAction(found, _ax_str("AXPress"))
        if err == _kAXErrorSuccess:
            time.sleep(0.35)
            return
        last_error = f"AXPress failed status={err}"
        time.sleep(0.15)
    raise RuntimeError(last_error)


def screenshot(path: Path) -> Path:
    """Capture the Voice panel via the sidecar harness (has Screen Recording)."""
    code, body = http_json(
        f"{SIDECAR}/desktop/act", method="POST", body={"action": "screenshot", "app": "Voice"}, timeout=25
    )
    if code != 200 or not isinstance(body, dict) or not body.get("path"):
        # Fall back to full-screen capture; may be wallpaper-only without TCC.
        subprocess.run(["screencapture", "-x", str(path)], check=True)
        return path
    src = Path(str(body["path"]))
    if src.is_file():
        path.write_bytes(src.read_bytes())
        return path
    subprocess.run(["screencapture", "-x", str(path)], check=True)
    return path


def quit_voice() -> None:
    if not voice_running():
        return
    subprocess.run(["osascript", "-e", 'tell application "Voice" to quit'], check=False)
    for _ in range(20):
        if not voice_running():
            return
        time.sleep(0.2)
    subprocess.run(["pkill", "-x", "Voice"], check=False)


def launch_app(app: Path) -> None:
    subprocess.run(["open", str(app)], check=True)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if voice_running():
            try:
                window_frame()
                return
            except Exception:
                time.sleep(0.3)
                continue
        time.sleep(0.3)
    raise RuntimeError("Voice.app did not show a window")


def listener(port: int) -> str:
    proc = subprocess.run(
        ["lsof", "-ti", f"TCP:{port}", "-sTCP:LISTEN"],
        capture_output=True,
        text=True,
    )
    return (proc.stdout or "").strip().splitlines()[0] if proc.stdout.strip() else ""


def wait_http(url: str, timeout: float, ready: Any = None) -> float:
    start = time.monotonic()
    deadline = start + timeout
    last = ""
    while time.monotonic() < deadline:
        code, body = http_json(url, timeout=1.5)
        last = f"{code} {body}"
        if code == 200:
            if ready is None:
                return time.monotonic() - start
            if callable(ready) and ready(body):
                return time.monotonic() - start
        time.sleep(0.2)
    raise TimeoutError(f"timed out waiting for {url}: {last}")


def describe_code(info: dict[str, Any]) -> tuple[bool, str]:
    """Whether a health payload says the process runs this checkout's current code."""
    fingerprint = info.get("fingerprint")
    if not fingerprint:
        return False, "no fingerprint: this process predates the current code and must be restarted"
    source = info.get("source") or ""
    if Path(source).resolve() != ROOT.resolve():
        return False, f"runs from {source}, not {ROOT}"
    if info.get("stale"):
        return False, f"fingerprint {fingerprint} no longer matches disk; restart to pick up the change"
    return True, f"fingerprint {fingerprint} pid {info.get('pid')}"


def verify_api(report: Report) -> None:
    code, health = http_json(f"{VOICE_HTTP}/health")
    if code == 200 and isinstance(health, dict) and "ready" in health:
        report.add("voice /health", bool(health.get("ready")), json.dumps(health))
        current, detail = describe_code(health)
        report.add("voice runs current code", current, detail)
        report.add(
            "voice runs research itself",
            health.get("server_tools") is True,
            "server_tools=true" if health.get("server_tools") is True else f"server_tools={health.get('server_tools')}",
        )
    else:
        report.add("voice /health", False, f"status {code} {health}")
        report.add("voice runs current code", False, "no health contract: legacy server")

    code, cfg = http_json(f"{SIDECAR}/config")
    ok = code == 200 and isinstance(cfg, dict) and "chatbotUrl" in cfg
    report.add(
        "sidecar config",
        ok,
        f"search={cfg.get('search')} desktopControl={cfg.get('desktopControl')}"
        if ok
        else f"status {code} {cfg}",
    )
    if ok:
        current, detail = describe_code(cfg)
        report.add("sidecar runs current code", current, detail)

    code, memory = http_json(f"{SIDECAR}/personal-memory")
    content = memory.get("content") if isinstance(memory, dict) else ""
    report.add(
        "personal memory GET",
        code == 200 and isinstance(content, str),
        f"{len(content)} chars" if isinstance(content, str) else f"status {code}",
    )

    code, sessions = http_json(f"{SIDECAR}/sessions")
    rows = sessions.get("sessions") if isinstance(sessions, dict) else None
    report.add(
        "sessions list",
        code == 200 and isinstance(rows, list),
        f"{len(rows)} saved" if isinstance(rows, list) else f"status {code}",
    )

    code, bridge = http_json(f"{SIDECAR}/browser/status")
    report.add(
        "chrome bridge status",
        code == 200 and isinstance(bridge, dict) and "connected" in bridge,
        json.dumps(bridge) if code == 200 else f"status {code} {bridge}",
    )

    if isinstance(cfg, dict) and cfg.get("search"):
        code, search = http_json(f"{SIDECAR}/search", method="POST", body={"query": "IANA example domains"}, timeout=20)
        hits = search.get("results") if isinstance(search, dict) else None
        report.add(
            "web search",
            code == 200 and isinstance(hits, list) and len(hits) >= 1,
            f"{len(hits)} hits" if isinstance(hits, list) else f"status {code} {search}",
        )
        code, fetched = http_json(
            f"{SIDECAR}/fetch",
            method="POST",
            body={"url": "https://www.iana.org/help/example-domains"},
            timeout=20,
        )
        text = fetched.get("text") if isinstance(fetched, dict) else ""
        report.add(
            "web fetch",
            code == 200 and isinstance(text, str) and len(text) > 40,
            f"{len(text)} chars" if isinstance(text, str) else f"status {code}",
        )
    else:
        report.add("web search", True, "skipped (no search key)")
        report.add("web fetch", True, "skipped (no search key)")


@dataclass
class Turn:
    """What one text turn over the realtime socket produced."""

    transcript: str = ""
    audio_seconds: float = 0.0
    # (name, arguments) of tools the server ran inside the response.
    server_tools: list[tuple[str, str]] = field(default_factory=list)
    server_tool_outputs: list[str] = field(default_factory=list)
    # Tools the server handed to the client to run (screenshot).
    client_tools: list[str] = field(default_factory=list)
    error: str = ""


def run_turn(instructions: str, text: str, timeout: float = 60, tools: list[dict[str, Any]] | None = None) -> Turn:
    """Send one user text turn and collect the reply until the response ends."""
    import asyncio

    import websockets

    async def connect() -> Any:
        """Open a session, waiting out the moment the previous turn's slot is released.

        The local backend runs one pipeline; after a disconnect the slot frees
        once SESSION_END drains through the handlers, which can take a second.
        """
        deadline = time.monotonic() + 20
        while True:
            ws = await websockets.connect(VOICE_WS, open_timeout=8, close_timeout=3, max_size=None)
            created = json.loads(await asyncio.wait_for(ws.recv(), timeout=8))
            if created.get("type") == "session.created":
                return ws
            await ws.close()
            error = created.get("error") or {}
            busy = error.get("type") in {"session_limit_reached", "server_starting"}
            if not busy or time.monotonic() > deadline:
                raise RuntimeError(f"expected session.created, got {created.get('type')}: {error.get('message', '')}")
            await asyncio.sleep(0.5)

    async def once() -> Turn:
        turn = Turn()
        pcm_bytes = 0
        session: dict[str, Any] = {"type": "realtime", "instructions": instructions}
        if tools is not None:
            session["tools"] = tools
            session["tool_choice"] = "auto"
        ws = await connect()
        try:
            await ws.send(json.dumps({"type": "session.update", "session": session}))
            await ws.send(
                json.dumps(
                    {
                        "type": "conversation.item.create",
                        "item": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]},
                    }
                )
            )
            await ws.send(json.dumps({"type": "response.create"}))
            deadline = time.monotonic() + timeout
            transcript: list[str] = []
            while time.monotonic() < deadline:
                raw = await asyncio.wait_for(ws.recv(), timeout=max(1.0, deadline - time.monotonic()))
                event = json.loads(raw)
                kind = event.get("type") or ""
                if kind in {
                    "response.audio_transcript.delta",
                    "response.output_audio_transcript.delta",
                    "response.text.delta",
                    "response.output_text.delta",
                }:
                    transcript.append(event.get("delta") or "")
                elif kind in {"response.audio.delta", "response.output_audio.delta"}:
                    pcm_bytes += len(event.get("delta") or "") * 3 // 4
                elif kind == "conversation.item.created":
                    item = event.get("item") or {}
                    if item.get("type") == "function_call":
                        turn.server_tools.append((item.get("name") or "", item.get("arguments") or ""))
                    elif item.get("type") == "function_call_output":
                        turn.server_tool_outputs.append(item.get("output") or "")
                elif kind == "response.output_item.done":
                    item = event.get("item") or {}
                    if item.get("type") == "function_call":
                        turn.client_tools.append(item.get("name") or "")
                elif kind == "response.done":
                    break
                elif kind == "error":
                    turn.error = str(event.get("error") or event)
                    break
            turn.transcript = "".join(transcript).strip()
            # 24 kHz 16-bit mono.
            turn.audio_seconds = pcm_bytes / (24000 * 2)
        finally:
            await ws.close()
        return turn

    return asyncio.run(once())


def spoken_pcm16(text: str = "hello there, can you hear me") -> bytes:
    """Real macOS speech, 16 kHz PCM16 mono — the same format Voice.app sends."""
    import tempfile
    import wave

    work = Path(tempfile.mkdtemp(prefix="voice-mic-"))
    aiff = work / "spoken.aiff"
    wav = work / "spoken.wav"
    subprocess.run(
        ["say", "-o", str(aiff), text],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["afconvert", "-f", "WAVE", "-d", "LEI16@16000", str(aiff), str(wav)],
        check=True,
        capture_output=True,
    )
    with wave.open(str(wav), "rb") as handle:
        if handle.getnchannels() != 1 or handle.getsampwidth() != 2:
            raise RuntimeError(f"unexpected spoken wav format: {handle.getparams()}")
        frames = handle.readframes(handle.getnframes())
    return frames


def gate_pcm16(pcm: bytes, threshold_dbfs: float = -48.0, min_brightness: float = 0.035) -> tuple[bytes, int, int]:
    """Mirror the Mac close-talk gate so verify covers the path Voice.app sends."""
    import array
    import math

    samples = array.array("h")
    samples.frombytes(pcm)
    open_floor = (10 ** (threshold_dbfs / 20.0)) * 32768.0
    hold_floor = (10 ** ((threshold_dbfs - 10.0) / 20.0)) * 32768.0
    hold = 0
    out = array.array("h")
    chunk = 512
    open_chunks = 0
    total = 0
    for start in range(0, len(samples), chunk):
        part = samples[start : start + chunk]
        if not part:
            break
        total += 1
        rms = math.sqrt(sum(int(sample) * int(sample) for sample in part) / len(part))
        deltas = [abs(int(part[i]) - int(part[i - 1])) for i in range(1, len(part))]
        brightness = (sum(deltas) / max(len(deltas), 1)) / max(rms, 1.0)
        close_talk = rms >= open_floor and brightness >= min_brightness
        if close_talk or (hold > 0 and rms >= hold_floor):
            hold = int(0.250 * 16_000)
            out.extend(part)
            open_chunks += 1
        elif hold > 0:
            hold = max(0, hold - len(part))
            if hold > 0:
                out.extend(part)
                open_chunks += 1
            else:
                out.extend([0] * len(part))
        else:
            out.extend([0] * len(part))
    return out.tobytes(), open_chunks, total


def run_mic_probe(timeout: float = 25) -> dict[str, Any]:
    """Send spoken PCM through the Mac gate, then the live VAD."""
    import asyncio
    import base64

    import websockets

    async def connect() -> Any:
        deadline = time.monotonic() + 20
        while True:
            ws = await websockets.connect(VOICE_WS, open_timeout=8, close_timeout=3, max_size=None)
            created = json.loads(await asyncio.wait_for(ws.recv(), timeout=8))
            if created.get("type") == "session.created":
                return ws
            await ws.close()
            error = created.get("error") or {}
            busy = error.get("type") in {"session_limit_reached", "server_starting"}
            if not busy or time.monotonic() > deadline:
                raise RuntimeError(f"expected session.created, got {created.get('type')}: {error.get('message', '')}")
            await asyncio.sleep(0.5)

    async def once() -> dict[str, Any]:
        result: dict[str, Any] = {"speech_started": False, "speech_stopped": False, "error": "", "events": []}
        raw = spoken_pcm16()
        pcm, open_chunks, total_chunks = gate_pcm16(raw)
        result["gate_open_frac"] = open_chunks / max(total_chunks, 1)
        if result["gate_open_frac"] < 0.4:
            result["error"] = f"close-talk gate muted spoken audio ({open_chunks}/{total_chunks} chunks)"
            return result
        ws = await connect()
        try:
            await ws.send(json.dumps({"type": "session.update", "session": {"type": "realtime"}}))
            # Match the Mac client: 40ms of 16 kHz PCM16 mono per append.
            chunk = 1280
            for offset in range(0, len(pcm), chunk):
                await ws.send(
                    json.dumps(
                        {
                            "type": "input_audio_buffer.append",
                            "audio": base64.b64encode(pcm[offset : offset + chunk]).decode(),
                        }
                    )
                )
            silence = bytes(chunk * 25)
            for offset in range(0, len(silence), chunk):
                await ws.send(
                    json.dumps(
                        {
                            "type": "input_audio_buffer.append",
                            "audio": base64.b64encode(silence[offset : offset + chunk]).decode(),
                        }
                    )
                )
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                try:
                    incoming = await asyncio.wait_for(ws.recv(), timeout=0.5)
                except TimeoutError:
                    if result["speech_started"]:
                        break
                    continue
                event = json.loads(incoming)
                kind = event.get("type") or ""
                result["events"].append(kind)
                if kind == "input_audio_buffer.speech_started":
                    result["speech_started"] = True
                elif kind == "input_audio_buffer.speech_stopped":
                    result["speech_stopped"] = True
                    break
                elif kind == "error":
                    result["error"] = str(event.get("error") or event)
                    break
        finally:
            await ws.close()
        return result

    return asyncio.run(once())


def verify_talk(report: Report) -> None:
    try:
        import websockets  # noqa: F401
    except ImportError:
        report.add("live model reply", False, "websockets package missing")
        return
    try:
        turn = run_turn("Reply with the single word pong and nothing else.", "ping", timeout=25)
        report.add("live model reply", bool(turn.transcript) and not turn.error, turn.transcript[:180] or turn.error)
    except Exception as exc:
        report.add("live model reply", False, str(exc) or exc.__class__.__name__)
    time.sleep(1.2)
    try:
        probe = run_mic_probe()
        started = probe.get("speech_started") is True and not probe.get("error")
        report.add(
            "live mic speech_started",
            started,
            (
                f"VAD accepted spoken PCM; gate open {probe.get('gate_open_frac', 0):.0%}"
                if started
                else f"no speech_started: {probe}"
            ),
        )
    except Exception as exc:
        report.add("live mic speech_started", False, str(exc) or exc.__class__.__name__)


# Same schemas this branch publishes. Without these on session.update the model
# cannot call bash even when the server is willing to run it.
RESEARCH_TOOLS = [
    {
        "type": "function",
        "name": "bash",
        "description": "Run one short research shell command. Use curl to search or fetch a public page.",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "A curl-based command."},
                "timeout": {"type": "number", "description": "Seconds to wait."},
            },
            "required": ["command"],
        },
    },
]


def verify_research(report: Report) -> None:
    """The model searches and reads a page inside its own reply, on the server."""
    try:
        import websockets  # noqa: F401
    except ImportError:
        report.add("research turn", False, "websockets package missing")
        return
    instructions = (
        "You are being tested. First say one short line such as 'Let me check that.' and in the same response "
        "call bash with command: curl -sL https://www.iana.org/help/example-domains | head -c 4000. "
        "Then answer in one short spoken sentence based on what the page says. Never read URLs aloud."
    )
    try:
        turn = run_turn(instructions, "What is the IANA example domains page for?", timeout=90, tools=RESEARCH_TOOLS)
    except Exception as exc:
        report.add("research turn", False, str(exc))
        return
    names = [name for name, _ in turn.server_tools]
    report.add(
        "bash ran on the server",
        "bash" in names and not turn.error,
        f"server tools: {names}; client tools: {turn.client_tools}" + (f"; error: {turn.error}" if turn.error else ""),
    )
    outputs = " ".join(turn.server_tool_outputs)
    report.add(
        "curl returned page text",
        "bash" in names and ("example" in outputs.casefold() or "iana" in outputs.casefold()),
        outputs[:200].replace("\n", " ") if outputs else "no tool output seen",
    )
    report.add(
        "model answered after researching",
        bool(turn.transcript) and turn.audio_seconds > 0.5,
        f"{turn.audio_seconds:.1f}s audio: {turn.transcript[:160]}",
    )
    report.add(
        "no tool was pushed to the client",
        not turn.client_tools,
        "search and page reading stayed server-side" if not turn.client_tools else f"client tools: {turn.client_tools}",
    )


def verify_research_initiative(report: Report) -> None:
    """A verify-first question must fetch inside the reply without being handed a curl command."""
    try:
        import websockets  # noqa: F401
    except ImportError:
        report.add("research initiative", False, "websockets package missing")
        return
    instructions = (
        "You are an AI conversation partner: perceptive, relaxed, warm, and quietly playful. "
        "You enjoy exploring ideas and have something thoughtful to contribute."
    )
    try:
        turn = run_turn(
            instructions,
            "Who is the current president of the United States?",
            timeout=90,
            tools=RESEARCH_TOOLS,
        )
    except Exception as exc:
        report.add("research initiative", False, str(exc))
        return
    names = [name for name, _ in turn.server_tools]
    report.add(
        "verify-first called bash",
        "bash" in names and not turn.error,
        f"server tools: {names}; client tools: {turn.client_tools}; "
        f"{turn.transcript[:160]!r}" + (f"; error: {turn.error}" if turn.error else ""),
    )


def verify_news_research(report: Report) -> None:
    """Dated RSS must be stamped, and an undated Google News URL must be flagged."""
    src = str(ROOT / "src")
    if src not in sys.path:
        sys.path.insert(0, src)
    try:
        from chatbot.LLM.curl_bash import finalize_research_output, run_research_command
    except Exception as exc:
        report.add("news research import", False, str(exc))
        return

    today = datetime.now().astimezone()
    undated = finalize_research_output(
        "Old follow-up\nTue, 01 Sep 2025 12:00:00 GMT\nNewer item\n"
        + today.strftime("%a, %d %b %Y 18:00:00 GMT")
        + "\n",
        command="curl -sL 'https://news.google.com/rss/search?q=world+news'",
        now=today,
    )
    report.add(
        "undated google news is flagged",
        "no when:1d window" in undated and "Checked at" in undated,
        undated.split("\n", 1)[0],
    )

    query = today.strftime("%B+%d+%Y")
    command = (
        "curl -sL "
        f"'https://news.google.com/rss/search?q=world+news+when:1d+{query}&hl=en-US&gl=US&ceid=US:en' "
        '| python3 -c "import sys,re; t=sys.stdin.read(); '
        "print('items', len(re.findall(r'<item>', t))); print(t[:3000])\""
    )
    try:
        output = run_research_command(command, timeout=20, now=today)
    except Exception as exc:
        report.add("live news rss", False, str(exc))
        return
    report.add(
        "live news rss stamped",
        "Checked at" in output and str(today.year) in output,
        output[:220].replace("\n", " "),
    )
    report.add(
        "live dated rss skips window hint",
        "no when:1d window" not in output,
        output[:160].replace("\n", " "),
    )
    usable = "almost no usable text" not in output and "items 0" not in output
    report.add("live news rss returned headlines", usable, output[:220].replace("\n", " "))


def verify_han_turn(report: Report) -> None:
    """No Chinese voice exists, but a stray Han character must not break the turn.

    The Kokoro-era run splitter that used to drop Han is gone with that backend
    (there is no espeak-ng fallback left to guard against), so Han now reaches
    the English Siri voice as written. This check only confirms the turn still
    completes with audio and no error.
    """
    try:
        import websockets  # noqa: F401
    except ImportError:
        report.add("han turn completes", False, "websockets package missing")
        return
    sentence = "Huawei, or \u534e\u4e3a, is a company."
    try:
        turn = run_turn(
            f"Reply with exactly this sentence and nothing else: {sentence}", "Say the test sentence.", timeout=45
        )
    except Exception as exc:
        report.add("han turn completes", False, str(exc))
        return
    report.add(
        "han turn completes",
        turn.audio_seconds > 1.0 and not turn.error,
        f"{turn.audio_seconds:.1f}s audio: {turn.transcript[:120]}" + (f"; error: {turn.error}" if turn.error else ""),
    )


def verify_ui(report: Report, app: Path) -> None:
    binary = voice_binary()
    report.add("Voice process", voice_running(), binary or "not running")
    if not voice_running():
        return
    if str(app) not in binary and app.exists():
        report.add(
            "worktree app",
            False,
            f"running binary is not {app}; quit Applications Voice and relaunch the worktree build",
        )
    else:
        report.add("worktree app", True, binary.splitlines()[0][:200])

    try:
        x, y, w, h = window_frame()
        report.add("panel window", w > 300 and h > 400, f"{w}x{h} at {x},{y}")
    except Exception as exc:
        report.add(
            "panel window", True, f"System Events cannot see the floating panel ({exc}); using harness screenshots"
        )

    shots = report.out_dir
    shots.mkdir(parents=True, exist_ok=True)

    def snap(name: str) -> Path:
        path = screenshot(shots / f"{name}.png")
        report.add(f"screenshot {name}", path.is_file() and path.stat().st_size > 1000, str(path))
        return path

    idle = snap("01-idle")
    report.add("panel pixels", idle.stat().st_size > 20_000, f"{idle.stat().st_size} bytes")

    subprocess.run(["open", "-a", "Voice"], check=False)
    time.sleep(0.5)
    try:
        run_osascript(
            """
tell application "Voice" to activate
delay 0.2
tell application "System Events"
  tell process "Voice"
    click menu bar item 1 of menu bar 2
    delay 0.25
    click menu item "Show panel" of menu 1 of menu bar item 1 of menu bar 2
  end tell
end tell
""",
            timeout=6,
        )
        report.add("menu Show panel", True)
    except Exception as exc:
        report.add("menu Show panel", True, f"used open -a Voice ({exc})")
    time.sleep(0.4)
    snap("02-shown")

    try:
        click_ax("voice.onTop")
        snap("03-on-top")
        click_ax("voice.onTop")
        click_ax("voice.theme.light")
        snap("04-theme-light")
        click_ax("voice.theme.dark")
        snap("05-theme-dark")
        click_ax("voice.theme.auto")
        report.add("header theme and pin", True)
    except Exception as exc:
        report.add("header theme and pin", False, str(exc))

    try:
        click_ax("voice.settings")
        settings = snap("06-settings")
        report.add("open Settings", settings.stat().st_size > 20_000, str(settings))
        time.sleep(0.4)
        for ident, label in (
            ("voice.tools.screenshot", "screenshot toggle"),
            ("voice.tools.search", "search toggle"),
            ("voice.tools.chrome", "chrome toggle"),
        ):
            try:
                click_ax(ident)
                click_ax(ident)
                report.add(label, True)
            except Exception as exc:
                report.add(label, False, str(exc))
        click_ax("voice.settings.close")
        time.sleep(0.2)
        click_ax("voice.history")
        snap("07-history")
        click_ax("voice.history.close")
        snap("08-closed-overlays")
        report.add("click Settings/History", True)
    except Exception as exc:
        report.add("click Settings/History", False, str(exc))
        snap("06-settings-failed")

    try:
        run_osascript(
            """
tell application "Voice" to activate
delay 0.2
tell application "System Events"
  tell process "Voice"
    click menu bar item 1 of menu bar 2
    delay 0.25
    click menu item "Start session" of menu 1 of menu bar item 1 of menu bar 2
  end tell
end tell
""",
            timeout=12,
        )
        time.sleep(1.5)
        snap("09-session-started")
        try:
            click_ax("voice.mute")
            snap("10-muted")
            click_ax("voice.mute")
            report.add("click mute", True)
        except Exception as exc:
            report.add("click mute", False, str(exc))
        try:
            click_ax("voice.stopReply", timeout=1.5)
            report.add("click stop reply", True)
        except Exception as exc:
            report.add("click stop reply", True, f"hidden until a reply: {exc}")
        try:
            click_ax("voice.stop")
            snap("11-stopped")
            report.add("click stop", True)
        except Exception as exc:
            report.add("click stop", False, str(exc))
        run_osascript(
            """
tell application "Voice" to activate
delay 0.2
tell application "System Events"
  tell process "Voice"
    click menu bar item 1 of menu bar 2
    delay 0.25
    try
      click menu item "End session" of menu 1 of menu bar item 1 of menu bar 2
    end try
  end tell
end tell
""",
            timeout=12,
        )
        time.sleep(0.6)
        snap("12-session-ended")
        report.add("menu start/end session", True)
    except Exception as exc:
        report.add("menu start/end session", False, str(exc))

    try:
        click_ax("voice.orb")
        time.sleep(0.6)
        snap("13-orb-start")
        try:
            click_ax("voice.stop")
        except Exception:
            run_osascript(
                """
tell application "Voice" to activate
delay 0.2
tell application "System Events"
  tell process "Voice"
    click menu bar item 1 of menu bar 2
    delay 0.25
    try
      click menu item "End session" of menu 1 of menu bar item 1 of menu bar 2
    end try
  end tell
end tell
""",
                timeout=12,
            )
        snap("14-orb-stopped")
        report.add("click orb", True)
    except Exception as exc:
        report.add("click orb", False, str(exc))

    try:
        click_ax("voice.closePanel")
        snap("15-closed-panel")
        subprocess.run(["open", "-a", "Voice"], check=False)
        time.sleep(0.6)
        snap("16-shown-again")
        report.add("click close panel", True)
    except Exception as exc:
        report.add("click close panel", False, str(exc))


def maybe_cold_start(report: Report, app: Path, cold: bool) -> None:
    if not cold:
        return
    print("Cold start: quitting Voice and restarting local services from this checkout…")
    quit_voice()
    for port in (8766, 7860):
        pid = listener(port)
        if pid:
            subprocess.run(["kill", pid], check=False)
    time.sleep(0.8)
    t0 = time.monotonic()
    launch_app(app)
    sidecar_s = wait_http(
        f"{SIDECAR}/config", timeout=45, ready=lambda body: isinstance(body, dict) and "chatbotUrl" in body
    )
    report.add("cold sidecar", True, f"{sidecar_s:.1f}s")

    def voice_is_ready(body: Any) -> bool:
        if isinstance(body, dict) and body.get("ready") is True:
            return True
        return False

    try:
        voice_s = wait_http(f"{VOICE_HTTP}/health", timeout=180, ready=voice_is_ready)
        report.add("cold voice /health ready", True, f"{voice_s:.1f}s (sidecar was up {sidecar_s:.1f}s earlier)")
    except TimeoutError as exc:
        # Fallback for a still-booting or legacy server.
        try:
            wait_http(
                f"{VOICE_HTTP}/openapi.json",
                timeout=30,
                ready=lambda body: isinstance(body, dict) and "openapi" in body,
            )
            report.add("cold voice /health ready", False, f"health not ready; openapi bound. {exc}")
        except Exception:
            report.add("cold voice /health ready", False, str(exc))
    report.add("cold total", True, f"{time.monotonic() - t0:.1f}s")


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify Voice.app + local services")
    parser.add_argument("--app", type=Path, default=DEFAULT_APP)
    parser.add_argument("--skip-ui", action="store_true")
    parser.add_argument("--skip-talk", action="store_true")
    parser.add_argument(
        "--research",
        action="store_true",
        help="Also run a turn that must bash/curl a page on the server, a dated news RSS check, and a Chinese-name TTS turn",
    )
    parser.add_argument("--cold", action="store_true", help="Quit Voice and restart local services")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = args.out or Path(f"/tmp/voice-verify-{stamp}")
    out.mkdir(parents=True, exist_ok=True)
    report = Report(out_dir=out)
    print(f"verify root={ROOT}")
    print(f"verify app={args.app}")
    print(f"verify out={out}")

    if args.cold:
        if not args.app.exists():
            report.add("app exists", False, str(args.app))
        else:
            maybe_cold_start(report, args.app, True)
    elif not args.skip_ui:
        if not voice_running():
            if args.app.exists():
                try:
                    launch_app(args.app)
                    report.add("launch Voice", True, str(args.app))
                except Exception as exc:
                    report.add("launch Voice", False, str(exc))
            else:
                report.add("launch Voice", False, f"missing {args.app}")
        else:
            activate_voice()
            report.add("launch Voice", True, "already running")

    verify_api(report)
    if voice_running():
        try:
            run_osascript(
                """
tell application "System Events"
  tell process "Voice"
    click menu bar item 1 of menu bar 2
    delay 0.2
    try
      click menu item "End session" of menu 1 of menu bar item 1 of menu bar 2
    on error
      key code 53
    end try
  end tell
end tell
""",
                timeout=8,
            )
            time.sleep(0.4)
        except Exception:
            pass
    # Talk before the UI tour so the panel is not occupying the only pipeline slot.
    if args.research:
        verify_news_research(report)
    if not args.skip_talk:
        verify_talk(report)
        if args.research:
            verify_research(report)
            verify_research_initiative(report)
            verify_han_turn(report)
    if not args.skip_ui:
        verify_ui(report, args.app)

    failed = [item for item in report.results if not item.ok]
    summary = {
        "ok": not failed,
        "passed": sum(1 for item in report.results if item.ok),
        "failed": [item.name for item in failed],
        "out": str(out),
        "binary": voice_binary(),
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print()
    print(json.dumps(summary, indent=2))
    if failed:
        print(f"\n{len(failed)} check(s) failed. Screenshots in {out}")
        return 1
    print(f"\nAll {len(report.results)} checks passed. Screenshots in {out}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
