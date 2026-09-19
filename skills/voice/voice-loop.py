#!/usr/bin/env python3
"""Pi /voice loop. Voice is ears and mouth. This process owns the turn slot."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import IO, Literal

ROOT = Path(__file__).resolve().parents[2]
VOICE_BIN = ROOT / "macos/Voice/build/Voice.app/Contents/MacOS/Voice"
FILLER_S = 1.5
FILLER_TEXT = "Let me check that."
SESSION_NAME = os.environ.get("VOICE_PI_SESSION", "voice-talk")


TurnKind = Literal["idle", "heard"]


class Turn:
    def __init__(self) -> None:
        self.kind: TurnKind = "idle"
        self.transcript = ""
        self.proc: subprocess.Popen[str] | None = None
        self.started = 0.0
        self.filler_sent = False


def emit(stream: IO[str], payload: dict[str, object]) -> None:
    stream.write(json.dumps(payload) + "\n")
    stream.flush()


def speak(voice: subprocess.Popen[str], text: str) -> None:
    assert voice.stdin is not None
    emit(voice.stdin, {"type": "speak", "text": text})


def quit_voice(voice: subprocess.Popen[str]) -> None:
    if voice.stdin and not voice.stdin.closed:
        try:
            emit(voice.stdin, {"type": "quit"})
        except BrokenPipeError:
            pass
    voice.terminate()


def cancel_pi(turn: Turn) -> None:
    if turn.proc is None:
        return
    turn.proc.terminate()
    try:
        turn.proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        turn.proc.kill()
    turn.proc = None
    turn.kind = "idle"


def main() -> int:
    os.environ["VOICE_THINKER"] = "pi"
    if not VOICE_BIN.is_file():
        print(f"voice-loop: build Voice first: {ROOT}/macos/Voice/scripts/build.sh", file=sys.stderr)
        return 2
    env = os.environ.copy()
    env["VOICE_THINKER"] = "pi"
    voice = subprocess.Popen(
        [str(VOICE_BIN), "--headless"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=sys.stderr,
        text=True,
        env=env,
    )
    assert voice.stdout is not None
    turn = Turn()
    lock = threading.Lock()

    def filler_watch() -> None:
        while voice.poll() is None:
            time.sleep(0.2)
            with lock:
                if turn.kind != "heard" or turn.filler_sent or turn.proc is None:
                    continue
                if time.monotonic() - turn.started < FILLER_S:
                    continue
                turn.filler_sent = True
                speak(voice, FILLER_TEXT)

    threading.Thread(target=filler_watch, daemon=True).start()

    def handle_line(raw: str) -> None:
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            return
        kind = event.get("type")
        if kind == "ready":
            print("voice: listening", flush=True)
            return
        if kind == "speech_started":
            with lock:
                cancel_pi(turn)
            return
        if kind != "transcript":
            return
        text = str(event.get("text") or "").strip()
        if not text:
            return
        if text.lower() in {"quit", "stop voice", "that's all"}:
            quit_voice(voice)
            return
        print(f"you: {text}", flush=True)
        with lock:
            cancel_pi(turn)
            turn.kind = "heard"
            turn.transcript = text
            turn.started = time.monotonic()
            turn.filler_sent = False
            turn.proc = subprocess.Popen(
                ["pi", "--print", "--session", SESSION_NAME, "--", text],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            proc = turn.proc

        def finish() -> None:
            assert proc.stdout is not None
            out, err = proc.communicate()
            reply = (out or "").strip() or (err or "").strip() or "I did not get an answer."
            with lock:
                if turn.proc is not proc:
                    return
                turn.proc = None
                turn.kind = "idle"
            print(f"pi: {reply}", flush=True)
            speak(voice, reply)

        threading.Thread(target=finish, daemon=True).start()

    def on_stop(_signum: int, _frame: object) -> None:
        quit_voice(voice)

    signal.signal(signal.SIGINT, on_stop)
    signal.signal(signal.SIGTERM, on_stop)

    for line in voice.stdout:
        handle_line(line)
    with lock:
        cancel_pi(turn)
    return 0 if voice.returncode in (0, None, -signal.SIGTERM) else voice.returncode or 1


if __name__ == "__main__":
    raise SystemExit(main())
