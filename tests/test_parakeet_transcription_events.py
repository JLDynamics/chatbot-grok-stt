import logging
import sys
from contextlib import contextmanager
from types import SimpleNamespace

import numpy as np

from chatbot.pipeline.messages import Transcription, VADAudio
from chatbot.STT import parakeet_tdt_handler
from chatbot.STT.parakeet_tdt_handler import ParakeetTDTSTTHandler


def test_process_yields_final_transcript(monkeypatch):
    handler = object.__new__(ParakeetTDTSTTHandler)
    handler.backend = "mlx"
    handler.last_language = "en"
    handler.start_language = None

    @contextmanager
    def fake_lock(*args, **kwargs):
        yield True

    handler._compute_lock_context = fake_lock
    handler._transcribe_turn = lambda audio_input, turn_id: ("I am here.", "en")
    monkeypatch.setattr(parakeet_tdt_handler.console, "print", lambda *args, **kwargs: None)

    result = list(handler.process(VADAudio(audio=np.zeros(16000, dtype=np.float32))))

    assert len(result) == 1
    assert isinstance(result[0], Transcription)
    assert result[0].text == "I am here."
    assert result[0].language_code == "en"


def test_final_transcription_lock_timeout_sets_error(monkeypatch):
    handler = object.__new__(ParakeetTDTSTTHandler)
    handler.backend = "mlx"
    handler.last_language = "en"
    handler.start_language = None

    @contextmanager
    def fake_lock(*args, **kwargs):
        yield False

    handler._compute_lock_context = fake_lock
    monkeypatch.setattr(parakeet_tdt_handler.console, "print", lambda *args, **kwargs: None)

    result = list(handler.process(VADAudio(audio=np.zeros(16000, dtype=np.float32))))
    assert len(result) == 1
    assert result[0].text == ""
    assert result[0].error == "stt_lock_timeout"


def test_parakeet_timing_logs_transcriptions():
    handler = object.__new__(ParakeetTDTSTTHandler)
    handler._times = [0.01]

    assert handler.timing_log_level == logging.INFO
    assert handler.should_log_timing(Transcription(text="I am here.", language_code="en"))


def test_iter_decode_windows_cuts_in_the_quiet_gap():
    sample_rate = 16_000
    # 5s of tone with a silent gap 0.4s before the 2s mark: the boundary must
    # land in the silence, not mid-tone, so no word is split across decodes.
    audio = np.ones(5 * sample_rate, dtype=np.float32)
    gap_start = 2 * sample_rate - int(0.4 * sample_rate)
    audio[gap_start : gap_start + int(0.1 * sample_rate)] = 0.0

    windows = parakeet_tdt_handler.iter_decode_windows(audio, sample_rate=sample_rate, max_s=2.0)

    first_len = len(windows[0])
    assert gap_start <= first_len <= gap_start + int(0.1 * sample_rate)
    # Contiguous and gap-free, so the per-window transcripts simply concatenate.
    assert sum(len(window) for window in windows) == len(audio)
    assert np.concatenate(windows).tolist() == audio.tolist()


def test_iter_decode_windows_keeps_windows_bounded():
    sample_rate = 16_000
    audio = np.ones(9 * sample_rate, dtype=np.float32)
    windows = parakeet_tdt_handler.iter_decode_windows(audio, sample_rate=sample_rate, max_s=2.0)
    assert all(len(window) <= 2 * sample_rate for window in windows)
    assert np.concatenate(windows).tolist() == audio.tolist()


def test_iter_decode_windows_keeps_short_audio_intact():
    audio = np.ones(8_000, dtype=np.float32)
    windows = parakeet_tdt_handler.iter_decode_windows(audio, sample_rate=16_000, max_s=2.0)
    assert len(windows) == 1
    assert windows[0] is audio


def test_decode_audio_joins_bounded_windows(monkeypatch):
    handler = object.__new__(ParakeetTDTSTTHandler)
    handler.sample_rate = 16_000
    seen = []

    def fake_decode(window):
        seen.append(len(window))
        return (f"part{len(seen)}", "en")

    handler._decode_mlx_window = fake_decode
    monkeypatch.setattr(
        parakeet_tdt_handler,
        "iter_decode_windows",
        lambda audio, sample_rate, max_s=30.0: [audio[:3], audio[3:]],
    )
    assert handler._decode_audio(np.zeros(8, dtype=np.float32)) == "part1 part2"
    assert seen == [3, 5]


def test_on_session_end_restores_the_configured_language():
    handler = object.__new__(ParakeetTDTSTTHandler)
    handler.start_language = None
    handler.last_language = "zh"

    handler.on_session_end()

    assert handler.last_language == "en"


def test_decode_releases_mlx_buffer_cache(monkeypatch):
    """Each decode must hand its activation buffers back.

    MLX keeps freed buffers in a reuse cache. Every revision of a pausing turn
    decodes a longer clip than the last, so the buffer sizes never match and the
    cache only grows -- measured 4.0 GB -> 11.4 GB over a minute of speech. The
    weights never grow, so nothing leaks; the cache just needs releasing.
    """
    handler = object.__new__(ParakeetTDTSTTHandler)
    handler.last_language = "en"
    handler.model = SimpleNamespace(decode_chunk=lambda audio, verbose=False: SimpleNamespace(text="hi"))

    cleared = []
    fake_mx = SimpleNamespace(
        array=lambda data, dtype=None: data,
        float32=None,
        clear_cache=lambda: cleared.append(True),
    )
    monkeypatch.setitem(sys.modules, "mlx.core", fake_mx)

    text, language = handler._decode_mlx_window(np.zeros(16, dtype=np.float32))

    assert text == "hi"
    assert language == "en"
    assert cleared == [True], "the decode must release MLX's buffer cache"


def _decoding_handler(transcripts=None):
    """Handler whose decode returns text keyed by how many samples it is given."""
    texts = transcripts or {}
    handler = object.__new__(ParakeetTDTSTTHandler)
    handler.sample_rate = 16_000
    handler.start_language = "en"
    handler.last_language = "en"
    handler._reset_turn_decode_cache()
    handler.decoded = []

    def decode(audio):
        handler.decoded.append(len(audio))
        return texts.get(len(audio), f"<{len(audio)}>")

    handler._decode_audio = decode
    return handler


def test_turn_decode_reuses_what_was_already_transcribed():
    # One turn, two pauses. VAD resends the whole turn each time, so the clips
    # grow: 16000 -> 24000 -> 28000 samples. Only the new audio may be decoded.
    handler = _decoding_handler({16_000: "hello there", 8_000: "and again", 4_000: "one more"})

    assert handler._transcribe_turn(np.zeros(16_000, dtype=np.float32), "turn_1")[0] == "hello there"
    assert handler._transcribe_turn(np.zeros(24_000, dtype=np.float32), "turn_1")[0] == "hello there and again"
    assert handler._transcribe_turn(np.zeros(28_000, dtype=np.float32), "turn_1")[0] == "hello there and again one more"

    # The first second, then only the 8000 and 4000 samples that were new --
    # never the earlier audio again.
    assert handler.decoded == [16_000, 8_000, 4_000]


def test_turn_decode_starts_over_for_a_new_turn():
    handler = _decoding_handler({})
    handler._transcribe_turn(np.zeros(16_000, dtype=np.float32), "turn_1")
    handler.decoded.clear()

    text, _ = handler._transcribe_turn(np.zeros(8_000, dtype=np.float32), "turn_2")

    assert handler.decoded == [8_000], "a new turn must decode its own audio from the start"
    assert text == "<8000>"


def test_turn_decode_recovers_audio_from_a_dropped_revision():
    """A revision dropped as stale leaves its audio past the cached mark.

    The next revision's audio still contains it, so it gets transcribed then --
    nothing spoken is lost.
    """
    handler = _decoding_handler({})
    handler._transcribe_turn(np.zeros(16_000, dtype=np.float32), "turn_1")
    handler.decoded.clear()

    # Revision 2 (24000 samples) never reached STT; revision 3 arrives at 40000.
    handler._transcribe_turn(np.zeros(40_000, dtype=np.float32), "turn_1")

    assert handler.decoded == [24_000], "must decode everything after the cached mark"


def test_turn_decode_falls_back_when_audio_shrinks():
    handler = _decoding_handler({})
    handler._transcribe_turn(np.zeros(32_000, dtype=np.float32), "turn_1")
    handler.decoded.clear()

    handler._transcribe_turn(np.zeros(16_000, dtype=np.float32), "turn_1")

    assert handler.decoded == [16_000], "a shorter clip is not an extension; decode it whole"
