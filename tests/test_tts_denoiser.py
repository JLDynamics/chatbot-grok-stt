"""Length-preservation tests for TTS SpectralDenoiser overlap-add."""

import numpy as np

from chatbot.TTS.tts_common import SpectralDenoiser


def _run(denoiser, chunks):
    parts = [denoiser.process(c) for c in chunks]
    parts.append(denoiser.flush())
    return np.concatenate([p for p in parts if len(p)])


def test_process_flush_preserves_length():
    rng = np.random.default_rng(0)
    # 3s of noisy speech-like signal in streaming chunks.
    t = np.arange(72000, dtype=np.float32) / 24000
    sig = (0.5 * np.sin(2 * np.pi * 220 * t) + 0.05 * rng.standard_normal(t.shape)).astype(np.float32)
    chunks = [sig[i : i + 997] for i in range(0, len(sig), 997)]  # odd sizes stress framing
    out = _run(SpectralDenoiser(), chunks)
    # Overlap-add keeps startup/transient cost to ~one frame, never ~4x stretch.
    assert abs(len(out) - len(sig)) < 2048


def test_flush_terminates_on_short_input():
    denoiser = SpectralDenoiser()
    out = _run(denoiser, [np.zeros(100, dtype=np.float32)])
    assert len(out) < 4096


def test_disabled_passthrough():
    denoiser = SpectralDenoiser(enabled=False)
    chunk = np.ones(5000, dtype=np.float32)
    assert np.array_equal(denoiser.process(chunk), chunk)
    assert len(denoiser.flush()) == 0


def test_output_tracks_input_content():
    # Non-stationary chirp: minimum-statistics NR latches onto endless pure
    # tones as "noise", so fidelity must be measured on speech-like input.
    rng = np.random.default_rng(1)
    t = np.arange(48000, dtype=np.float32) / 24000
    freq = 200 + 600 * t / t[-1]
    phase = 2 * np.pi * np.cumsum(freq) / 24000
    sig = (0.5 * np.sin(phase) + 0.05 * rng.standard_normal(t.shape)).astype(np.float32)
    out = _run(SpectralDenoiser(), [sig])
    n = min(len(sig), len(out))
    corr = float(np.corrcoef(sig[4096:n], out[4096:n])[0, 1])
    assert corr > 0.97
