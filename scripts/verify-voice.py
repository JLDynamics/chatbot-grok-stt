#!/usr/bin/env python3
"""Click-through and API verify for the native Voice panel.

Exercises the places the refactor changed: sidecar/voice health, settings
wiring (memory, sessions, search, chrome bridge, screenshot, code agent),
header/control buttons, and a short live model reply.

Usage (from the repo root):

    python3 scripts/verify-voice.py
    python3 scripts/verify-voice.py --app macos/Voice/build/Voice.app
    python3 scripts/verify-voice.py --skip-ui
    python3 scripts/verify-voice.py --skip-ui --research   # server-side search/read_page + Chinese TTS turns
    python3 scripts/verify-voice.py --cold     # quit Voice, restart local services

Also confirms both local services run this checkout's current code (their
health payloads carry a source fingerprint), which is how a stale backend
that silently lacked server-side search was caught.

Does not overwrite personal memory or delete saved conversations.
"""

from __future__ import annotations

import argparse
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
            "voice runs search/read_page itself",
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
        f"search={cfg.get('search')} codeAgent={cfg.get('codeAgent')} desktopControl={cfg.get('desktopControl')}"
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
    # Tools the server handed to the client to run (screenshot, code_agent).
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
        report.add("live model reply", False, str(exc))


# Same schemas Voice.app publishes. Without these on session.update the model
# cannot call search or read_page even when the server is willing to run them.
RESEARCH_TOOLS = [
    {
        "type": "function",
        "name": "web_search",
        "description": "Search the web for current or specific facts. Returns titles, snippets and URLs.",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "The search query."}},
            "required": ["query"],
        },
    },
    {
        "type": "function",
        "name": "read_page",
        "description": "Read the full text of a web page: a URL you know or found with web_search.",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Public URL, if known."},
                "prefer_browser": {"type": "boolean", "description": "Start from the user's Chrome."},
            },
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
        "call web_search with the query 'IANA example domains'. Then call read_page on the most relevant result. "
        "Finally answer in one short spoken sentence based on what the page says. Never read URLs aloud."
    )
    try:
        turn = run_turn(instructions, "What is the IANA example domains page for?", timeout=90, tools=RESEARCH_TOOLS)
    except Exception as exc:
        report.add("research turn", False, str(exc))
        return
    names = [name for name, _ in turn.server_tools]
    report.add(
        "web_search ran on the server",
        "web_search" in names and not turn.error,
        f"server tools: {names}; client tools: {turn.client_tools}" + (f"; error: {turn.error}" if turn.error else ""),
    )
    outputs = " ".join(turn.server_tool_outputs)
    report.add(
        "search returned results",
        "web_search" in names and "URL:" in outputs,
        outputs[:200].replace("\n", " ") if outputs else "no tool output seen",
    )
    report.add(
        "read_page ran on the server",
        any(name in {"read_page", "read_url", "fetch_page", "web_fetch"} for name in names),
        f"server tools: {names}",
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


def server_log_since(marker_offset: int) -> str:
    log = Path(os.environ.get("SERVER_LOG", "/tmp/chatbot-server.log"))
    try:
        with log.open("rb") as handle:
            handle.seek(marker_offset)
            return handle.read().decode("utf-8", "replace")
    except OSError:
        return ""


def server_log_size() -> int:
    log = Path(os.environ.get("SERVER_LOG", "/tmp/chatbot-server.log"))
    try:
        return log.stat().st_size
    except OSError:
        return 0


def verify_chinese_tts(report: Report) -> None:
    """A Chinese name inside an English reply is synthesized by the Mandarin pipeline."""
    try:
        import websockets  # noqa: F401
    except ImportError:
        report.add("chinese tts turn", False, "websockets package missing")
        return
    sentence = "Huawei, or 华为, is a Chinese company."
    offset = server_log_size()
    try:
        turn = run_turn(
            f"Reply with exactly this sentence and nothing else: {sentence}", "Say the test sentence.", timeout=45
        )
    except Exception as exc:
        report.add("chinese tts turn", False, str(exc))
        return
    report.add(
        "chinese tts turn",
        "华为" in turn.transcript and turn.audio_seconds > 1.0 and not turn.error,
        f"{turn.audio_seconds:.1f}s audio: {turn.transcript[:120]}" + (f"; error: {turn.error}" if turn.error else ""),
    )
    time.sleep(0.5)
    log = server_log_since(offset)
    spliced = "Kokoro mixed-script reply" in log and "[z]" in log
    report.add(
        "chinese run used the mandarin pipeline",
        spliced,
        "server log shows the reply spliced across the English and Mandarin pipelines"
        if spliced
        else "server log has no mixed-script splice for this turn (old backend, or log not at $SERVER_LOG)",
    )
    report.add(
        "no espeak fallback on chinese characters",
        "words count mismatch" not in log,
        "phonemizer never saw the Chinese characters"
        if "words count mismatch" not in log
        else "phonemizer fallback fired: the English pipeline received Chinese characters",
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

    # Menu extra is reliable even when the floating panel is not in System Events.
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
            timeout=12,
        )
        report.add("menu Show panel", True)
    except Exception as exc:
        report.add("menu Show panel", False, str(exc))
    time.sleep(0.4)
    snap("02-shown")

    try:
        click_panel_button(6)  # Settings
        settings = snap("03-settings")
        report.add("open Settings", settings.stat().st_size > 20_000, str(settings))
        click_panel_button(1)  # overlay close is the first button in the overlay tree
        time.sleep(0.2)
        click_panel_button(5)  # Conversations
        snap("04-history")
        click_panel_button(1)
        snap("05-closed-overlays")
    except Exception as exc:
        report.add("click Settings/History", False, str(exc))
        snap("03-settings-failed")

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
        snap("06-session-started")
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
        snap("07-session-ended")
        report.add("menu start/end session", True)
    except Exception as exc:
        report.add("menu start/end session", False, str(exc))


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
        help="Also run a turn that must web_search + read_page on the server, and a Chinese-name TTS turn",
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
    if not args.skip_talk:
        verify_talk(report)
        if args.research:
            verify_research(report)
            verify_chinese_tts(report)
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
