"""Shared TTS turn helpers."""

from __future__ import annotations

from collections import deque
from typing import Any

import numpy as np

from chatbot.pipeline.messages import TTSInput


class SpectralDenoiser:
    """Causal, low-latency spectral noise reduction (Wiener / spectral gating).

    VibeVoice carries a faint but constant hiss that sits *inside* the voice
    (an elevated inter-harmonic noise floor), so a conventional noise gate
    (which only touches near-silence) cannot remove it. This denoises the STFT
    of the stream: it keeps a per-bin noise estimate from recent frames (a
    low-percentile "minimum statistics" style floor) and applies a Wiener gain
    ``(P - N)/P`` clamped to a floor so nothing collapses to silence. It is
    causal (only past frames), so it streams with roughly one frame of latency,
    and it leaves the speech largely untouched.
    """

    def __init__(
        self,
        enabled: bool = True,
        frame: int = 1024,
        hop: int = 256,
        floor: float = 0.10,
        noise_pct: float = 10.0,
        lookback: int = 24,
    ) -> None:
        self.enabled = enabled
        self.frame = frame
        self.hop = hop
        self.floor = float(floor)
        self.noise_pct = float(noise_pct)
        self.lookback = max(1, int(lookback))
        self._win = np.hanning(frame).astype(np.float32)
        self._in = np.zeros(0, dtype=np.float32)
        self._maghist: deque[np.ndarray] = deque(maxlen=self.lookback)
        self._acc = np.zeros(0, dtype=np.float32)
        self._wacc = np.zeros(0, dtype=np.float32)

    def _process_frame(self, frame: np.ndarray) -> np.ndarray:
        mag = np.abs(np.fft.rfft(frame * self._win))
        phase = np.angle(np.fft.rfft(frame * self._win))
        self._maghist.append(mag.copy())
        hist = np.array(self._maghist)
        noise = np.percentile(hist, self.noise_pct, axis=0)
        p = mag * mag
        nn = noise * noise
        gain = np.clip((p - nn) / (p + 1e-12), self.floor, 1.0)
        spec = (mag * gain) * np.exp(1j * phase)
        return np.fft.irfft(spec, self.frame) * self._win

    def process(self, chunk: np.ndarray) -> np.ndarray:
        if not self.enabled or chunk is None or chunk.size == 0:
            return np.asarray(chunk, dtype=np.float32)
        self._in = np.concatenate([self._in, np.asarray(chunk, dtype=np.float32)])
        out = np.zeros(0, dtype=np.float32)
        while len(self._in) >= self.frame:
            yf = self._process_frame(self._in[: self.frame])
            self._in = self._in[self.hop :]
            self._pad_acc()
            self._acc[: self.frame] += yf
            self._wacc[: self.frame] += self._win * self._win
            out = np.concatenate([out, self._emit_hop()])
        return out

    def flush(self) -> np.ndarray:
        if not self.enabled:
            return np.zeros(0, dtype=np.float32)
        out = np.zeros(0, dtype=np.float32)
        while len(self._in) > 0:
            fr = self._in[: self.frame]
            if len(fr) < self.frame:
                fr = np.pad(fr, (0, self.frame - len(fr)))
            yf = self._process_frame(fr)
            self._in = self._in[self.hop :] if len(self._in) > self.hop else np.zeros(0, dtype=np.float32)
            self._pad_acc()
            self._acc[: self.frame] += yf
            self._wacc[: self.frame] += self._win * self._win
            out = np.concatenate([out, self._emit_hop()])
        # Drain the overlap tail left by the final frames (real signal).
        while len(self._acc) > 0:
            out = np.concatenate([out, self._emit_hop()])
        return out

    def _pad_acc(self) -> None:
        # NOTE: extend by hop, not frame. Extending by frame while shifting by
        # hop grows the accumulator 3x beyond the signal, and dumping it in
        # flush() used to stretch every reply ~4x (22s of audio for 5s of
        # speech), keeping the mic ducked long after the words ended.
        if len(self._acc) < self.frame:
            pad = self.frame - len(self._acc)
            self._acc = np.concatenate([self._acc, np.zeros(pad, dtype=np.float32)])
            self._wacc = np.concatenate([self._wacc, np.zeros(pad, dtype=np.float32)])

    def _emit_hop(self) -> np.ndarray:
        n = min(self.hop, len(self._acc))
        emit = self._acc[:n].copy()
        denom = self._wacc[:n].copy()
        self._acc = self._acc[n:]
        self._wacc = self._wacc[n:]
        return emit / np.where(denom > 1e-8, denom, 1.0)


class TTSNoiseGate:
    """A gentle downward expander that hides the TTS output noise floor.

    The model output has a faint constant hiss (roughly 30 dB below the
    speech). During pauses and very quiet moments that hiss is what you hear as
    "background noise". ``process`` attenuates the signal only when its smoothed
    RMS envelope is well below the speech level, leaving the voice untouched
    while pushing the pauses towards silence. A soft knee, a floor gain and a
    fast-attack / slow-release envelope keep it from sounding choppy.
    """

    def __init__(
        self,
        enabled: bool = True,
        threshold: float = 0.010,
        ratio: float = 2.0,
        floor_gain: float = 0.15,
        attack: float = 0.40,
        release: float = 0.03,
    ) -> None:
        self.enabled = enabled
        self.threshold = float(threshold)
        self.ratio = float(ratio)
        self.floor_gain = float(floor_gain)
        self.attack = float(attack)
        self.release = float(release)
        self._env = 0.0
        self._started = False

    def process(self, block: np.ndarray) -> np.ndarray:
        if not self.enabled or block is None or block.size == 0:
            return block
        block = block.astype(np.float32, copy=False)
        rms = float(np.sqrt(np.mean(block**2)))
        if self._started:
            coef = self.attack if rms > self._env else self.release
            self._env += coef * (rms - self._env)
        else:
            self._env = rms
            self._started = True
        if self._env >= self.threshold:
            return block
        # Soft-knee downward expansion: below the threshold, scale towards the
        # floor gain; at/above it, pass-through.
        gain = self.floor_gain + (1.0 - self.floor_gain) * (min(self._env, self.threshold) / self.threshold) ** self.ratio
        return block * min(1.0, gain)


def drop_queued_tts_inputs(queue_in: Any, turn_id: str | None, turn_revision: int | None) -> int:
    """Remove queued ``TTSInput``s for the failed turn so they cannot restart it."""
    if not hasattr(queue_in, "mutex") or not hasattr(queue_in, "queue"):
        return 0
    dropped = 0
    with queue_in.mutex:
        kept: deque[Any] = deque()
        for item in queue_in.queue:
            if isinstance(item, TTSInput) and (item.turn_id, item.turn_revision) == (turn_id, turn_revision):
                dropped += 1
                continue
            kept.append(item)
        queue_in.queue = kept
    return dropped
