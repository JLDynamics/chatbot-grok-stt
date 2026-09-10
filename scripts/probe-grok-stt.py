#!/usr/bin/env python3
"""Probe whether a Grok CLI subscription token is accepted by xAI's STT socket.

Answers one question before any real work is built on it: does the OIDC token
the Grok CLI stores in ~/.grok/auth.json authenticate wss://api.x.ai/v1/stt?
The published docs only describe API-key auth, so this path is undocumented and
could stop working without notice.

The token is read from disk and put in a header. It is never printed.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import pathlib
import sys
import wave

STT_URL = "wss://api.x.ai/v1/stt"
AUTH_PATH = pathlib.Path.home() / ".grok" / "auth.json"


def load_token(path: pathlib.Path = AUTH_PATH) -> tuple[str, str]:
    """Newest still-valid bearer token from the Grok CLI's auth file."""
    entries = json.loads(path.read_text())
    best: tuple[dt.datetime, str, str] | None = None
    for key, entry in entries.items():
        token = entry.get("key")
        if not token:
            continue
        raw = entry.get("expires_at", "")
        try:
            expires = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        if best is None or expires > best[0]:
            best = (expires, token, key)
    if best is None:
        raise SystemExit(f"no usable token in {path}")
    expires, token, issuer = best
    if expires <= dt.datetime.now(dt.timezone.utc):
        raise SystemExit(f"token expired at {expires.isoformat()}; run `grok` to refresh it")
    return token, f"{issuer} (expires {expires.isoformat(timespec='seconds')})"


def read_pcm16(path: pathlib.Path) -> tuple[bytes, int]:
    with wave.open(str(path), "rb") as w:
        if w.getsampwidth() != 2 or w.getnchannels() != 1:
            raise SystemExit("probe expects mono 16-bit PCM wav")
        return w.readframes(w.getnframes()), w.getframerate()


async def probe(pcm: bytes, sample_rate: int, frame_ms: int, realtime: bool) -> int:
    from websockets.asyncio.client import connect

    token, who = load_token()
    print(f"  token source : {who}")
    url = f"{STT_URL}?sample_rate={sample_rate}&encoding=pcm&interim_results=true&language=en"
    print(f"  connecting   : {STT_URL}")

    frame_bytes = int(sample_rate * frame_ms / 1000) * 2
    finals: list[str] = []
    try:
        async with connect(url, additional_headers={"Authorization": f"Bearer {token}"}) as ws:
            print("  handshake    : accepted\n")

            async def send_audio() -> None:
                for i in range(0, len(pcm), frame_bytes):
                    await ws.send(pcm[i : i + frame_bytes])
                    if realtime:
                        await asyncio.sleep(frame_ms / 1000)
                # Tell the server the utterance is over, then let it flush.
                await ws.send(json.dumps({"type": "commit"}))

            sender = asyncio.create_task(send_audio())
            try:
                while True:
                    raw = await asyncio.wait_for(ws.recv(), timeout=15)
                    if isinstance(raw, bytes):
                        continue
                    event = json.loads(raw)
                    text = event.get("text") or event.get("transcript") or ""
                    final = event.get("is_final")
                    label = "FINAL " if final else "partial"
                    if text:
                        print(f"  {label}: {text}")
                    if final and text:
                        finals.append(text)
                    if event.get("type") in {"done", "close", "end"}:
                        break
            except asyncio.TimeoutError:
                print("  (no more events for 15s)")
            finally:
                sender.cancel()
    except Exception as exc:  # noqa: BLE001 - the point is to report the failure
        name = exc.__class__.__name__
        print(f"\n  REJECTED: {name}: {exc}")
        print("  -> the subscription token is not accepted for STT on this path")
        return 1

    print(f"\n  transcript: {' '.join(finals) if finals else '(none)'}")
    print("  -> the subscription token IS accepted for STT")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("wav", type=pathlib.Path, help="mono 16-bit PCM wav to send")
    ap.add_argument("--frame-ms", type=int, default=100)
    ap.add_argument("--realtime", action="store_true", help="pace frames like a live mic")
    args = ap.parse_args()

    pcm, sample_rate = read_pcm16(args.wav)
    print(f"  audio        : {args.wav.name}, {len(pcm) / 2 / sample_rate:.1f}s at {sample_rate} Hz")
    return asyncio.run(probe(pcm, sample_rate, args.frame_ms, args.realtime))


if __name__ == "__main__":
    sys.exit(main())
