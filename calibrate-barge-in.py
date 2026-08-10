#!/usr/bin/env python
"""Find a barge-in level that separates your voice from the assistant's echo.

On a speaker setup the mic hears the assistant. Silero scores that bleed as
speech no matter how quiet it is, so the VAD threshold cannot separate the two;
only loudness can. This measures both and prints a level to use.

    .venv/bin/python calibrate-barge-in.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np
import sounddevice as sd
import soundfile as sf

DUR = 4.0
RATE = 16000


def resolve(spec: str, kind: str) -> tuple[int | None, str]:
    key = "max_input_channels" if kind == "in" else "max_output_channels"
    devices = sd.query_devices()
    usable = [(i, d) for i, d in enumerate(devices) if d[key] > 0]
    for name in [n.strip() for n in spec.split("|") if n.strip()]:
        for i, d in usable:
            if d["name"] == name:
                return i, d["name"]
        for i, d in usable:
            if name.lower() in d["name"].lower():
                return i, d["name"]
    return None, "system default"


def record(seconds: float, device: int | None) -> np.ndarray:
    buf = sd.rec(int(seconds * RATE), samplerate=RATE, channels=1,
                 dtype="float32", device=device)
    sd.wait()
    a = np.asarray(buf).flatten()
    return a[~np.isnan(a)]


def peak(a: np.ndarray) -> float:
    return float(np.abs(a).max()) if a.size else 0.0


def main() -> int:
    mic_spec = sys.argv[1] if len(sys.argv) > 1 else "ZTD39|MacBook Pro Microphone"
    spk_spec = sys.argv[2] if len(sys.argv) > 2 else "SAMSUNG|MacBook Pro Speakers"
    mic_idx, mic_name = resolve(mic_spec, "in")
    spk_idx, spk_name = resolve(spk_spec, "out")
    print(f"mic: {mic_name}\nout: {spk_name}\n")

    sample = next(
        (p for p in [Path("/tmp/barge-in-sample.wav")] if p.exists()),
        None,
    )

    # 1. How loud is the assistant's bleed?
    print(f"[1/2] Measuring the assistant's echo. Stay quiet for {DUR:.0f}s...")
    time.sleep(0.6)
    if sample is not None:
        w, sr = sf.read(str(sample), dtype="float32")
        w = w.flatten()[: int(DUR * sr)]
    else:  # fall back to a speech-like tone sweep if no clip is available
        sr = 24000
        t = np.linspace(0, DUR, int(DUR * sr), endpoint=False)
        w = (0.6 * np.sin(2 * np.pi * 180 * t) * (0.5 + 0.5 * np.sin(2 * np.pi * 3 * t))).astype("float32")
    rec = sd.playrec(w, samplerate=sr, channels=1, input_mapping=[1],
                     device=(mic_idx, spk_idx), dtype="float32")
    sd.wait()
    bleed_arr = np.asarray(rec).flatten()
    bleed = peak(bleed_arr[~np.isnan(bleed_arr)])
    print(f"      echo peak: {bleed:.3f}\n")

    # 2. How loud is the user?
    print(f"[2/2] Now TALK NORMALLY for {DUR:.0f}s (say anything)...")
    time.sleep(0.6)
    voice = peak(record(DUR, mic_idx))
    print(f"      your voice peak: {voice:.3f}\n")

    print("-" * 52)
    if voice <= bleed * 1.3:
        print("NO WORKABLE GAP.")
        print(f"  Your voice ({voice:.3f}) is not clearly louder than the echo ({bleed:.3f}).")
        print("  Turn the speakers down, move the mic away, or use headphones")
        print("  (headphones remove the echo entirely and need no threshold).")
        return 1

    level = round(bleed + (voice - bleed) * 0.45, 2)
    print(f"RECOMMENDED:  BARGE_IN_LEVEL={level}")
    print(f"  echo {bleed:.3f}  <  {level}  <  your voice {voice:.3f}")
    print()
    print(f"  Try it:  BARGE_IN_LEVEL={level} ./run-openrouter.sh")
    print("  Too eager to interrupt itself? Raise it. Not hearing you? Lower it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
