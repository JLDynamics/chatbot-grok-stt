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
        # Overlap-add weights are the squared window; it never changes.
        self._win2 = self._win * self._win
        self._in = np.zeros(0, dtype=np.float32)
        # Recent per-bin magnitudes as a ``(bins, lookback)`` ring buffer. Bins
        # are rows so the noise percentile partitions along a contiguous axis,
        # and no per-frame copy of the history is needed.
        self._hist: np.ndarray | None = None
        self._hist_pos = 0
        self._hist_len = 0
        self._acc = np.zeros(0, dtype=np.float32)
        self._wacc = np.zeros(0, dtype=np.float32)

    def _remember_magnitude(self, mag: np.ndarray) -> None:
        # Match the magnitude dtype exactly: rfft on float32 audio yields
        # complex64, and widening the history here would silently promote the
        # noise estimate (and the gain derived from it) to float64.
        if self._hist is None or self._hist.shape != (mag.shape[0], self.lookback) or self._hist.dtype != mag.dtype:
            self._hist = np.empty((mag.shape[0], self.lookback), dtype=mag.dtype)
            self._hist_pos = 0
            self._hist_len = 0
        self._hist[:, self._hist_pos] = mag
        self._hist_pos = (self._hist_pos + 1) % self.lookback
        self._hist_len = min(self._hist_len + 1, self.lookback)

    def _noise_estimate(self) -> np.ndarray:
        """``np.percentile(history, noise_pct, axis=frames)`` on the ring buffer.

        Percentiles ignore row order, so the rotated ring buffer gives the same
        answer as the chronological history; this selects the two bracketing
        order statistics directly instead of running the general percentile
        machinery on a freshly stacked copy every frame.
        """
        assert self._hist is not None
        n = self._hist_len
        hist = self._hist if n == self.lookback else self._hist[:, :n]
        if n == 1:
            return hist[:, 0].copy()
        virtual_index = (self.noise_pct / 100.0) * (n - 1)
        lower = int(np.floor(virtual_index))
        upper = min(lower + 1, n - 1)
        frac = virtual_index - lower
        # A full sort of each short, contiguous lookback row beats a partial
        # selection here: introselect's per-row setup costs more than sorting
        # ~24 values outright.
        ordered = np.sort(hist, axis=-1)
        if lower == upper or frac == 0.0:
            return ordered[:, lower].copy()
        low_value = ordered[:, lower]
        high_value = ordered[:, upper]
        span = high_value - low_value
        # Mirrors numpy's linear interpolation, which anchors on whichever end
        # is nearer so the result stays exact at the bracketing samples.
        if frac < 0.5:
            return low_value + span * frac
        return high_value - span * (1.0 - frac)

    def _process_frame(self, frame: np.ndarray) -> np.ndarray:
        spec = np.fft.rfft(frame * self._win)
        mag = np.abs(spec)
        phase = np.angle(spec)
        self._remember_magnitude(mag)
        noise = self._noise_estimate()
        p = mag * mag
        nn = noise * noise
        gain = np.clip((p - nn) / (p + 1e-12), self.floor, 1.0)
        spec = (mag * gain) * np.exp(1j * phase)
        return np.fft.irfft(spec, self.frame) * self._win

    def process(self, chunk: np.ndarray) -> np.ndarray:
        if not self.enabled or chunk is None or chunk.size == 0:
            return np.asarray(chunk, dtype=np.float32)
        self._in = np.concatenate([self._in, np.asarray(chunk, dtype=np.float32)])
        parts: list[np.ndarray] = []
        while len(self._in) >= self.frame:
            yf = self._process_frame(self._in[: self.frame])
            self._in = self._in[self.hop :]
            self._pad_acc()
            self._acc[: self.frame] += yf
            self._wacc[: self.frame] += self._win2
            parts.append(self._emit_hop())
        if not parts:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(parts)

    def flush(self) -> np.ndarray:
        if not self.enabled:
            return np.zeros(0, dtype=np.float32)
        parts: list[np.ndarray] = []
        while len(self._in) > 0:
            fr = self._in[: self.frame]
            if len(fr) < self.frame:
                fr = np.pad(fr, (0, self.frame - len(fr)))
            yf = self._process_frame(fr)
            self._in = self._in[self.hop :] if len(self._in) > self.hop else np.zeros(0, dtype=np.float32)
            self._pad_acc()
            self._acc[: self.frame] += yf
            self._wacc[: self.frame] += self._win2
            parts.append(self._emit_hop())
        # Drain the overlap tail left by the final frames (real signal).
        while len(self._acc) > 0:
            parts.append(self._emit_hop())
        if not parts:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(parts)

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
        # The division allocates the result, so the accumulator slices can stay
        # views: nothing aliases ``_acc``/``_wacc`` once this returns.
        emit = self._acc[:n] / np.where(self._wacc[:n] > 1e-8, self._wacc[:n], 1.0)
        self._acc = self._acc[n:]
        self._wacc = self._wacc[n:]
        return emit


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


def build_denoise_chain(
    gen_kwargs: dict[str, Any],
) -> tuple[SpectralDenoiser, TTSNoiseGate, dict[str, Any]]:
    """Pop the shared denoise flags and build the (denoiser, gate) pair."""
    gen_kwargs = dict(gen_kwargs)
    spectral_enabled = bool(gen_kwargs.pop("spectral_denoise", True))
    spectral_floor = float(gen_kwargs.pop("spectral_denoise_floor", 0.04))
    denoiser = SpectralDenoiser(enabled=spectral_enabled, floor=spectral_floor)
    noise_gate_enabled = bool(gen_kwargs.pop("noise_gate", True))
    noise_gate_threshold = float(gen_kwargs.pop("noise_gate_threshold", 0.010))
    gate = TTSNoiseGate(enabled=noise_gate_enabled, threshold=noise_gate_threshold)
    return denoiser, gate, gen_kwargs
