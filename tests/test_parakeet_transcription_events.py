import io
import logging
from contextlib import contextmanager
from types import SimpleNamespace

import numpy as np
from rich.text import Text

from chatbot.pipeline.messages import PartialTranscription, Transcription, VADAudio
from chatbot.STT import parakeet_tdt_handler
from chatbot.STT.parakeet_tdt_handler import ParakeetTDTSTTHandler
from chatbot.STT.smart_progressive_streaming import SmartProgressiveStreamingHandler


def test_show_progressive_transcription_returns_combined_text(monkeypatch):
    handler = object.__new__(ParakeetTDTSTTHandler)
    handler.streaming_handler = SimpleNamespace(
        transcribe_incremental=lambda audio: SimpleNamespace(
            fixed_text="I just wanted",
            active_text="to check in",
        )
    )
    monkeypatch.setattr(parakeet_tdt_handler.console, "print", lambda *args, **kwargs: None)

    result = handler._show_progressive_transcription(np.zeros(16000, dtype=np.float32))

    assert result == "I just wanted to check in"


def test_show_progressive_transcription_prefixes_reopened_fragment(monkeypatch):
    handler = object.__new__(ParakeetTDTSTTHandler)
    handler._live_prefix_text = "yeah i still need to finish"
    handler.streaming_handler = SimpleNamespace(
        transcribe_incremental=lambda audio: SimpleNamespace(
            fixed_text="",
            active_text="a lot of work to do",
        )
    )
    monkeypatch.setattr(parakeet_tdt_handler.console, "print", lambda *args, **kwargs: None)

    result = handler._show_progressive_transcription(np.zeros(16000, dtype=np.float32))

    assert result == "yeah i still need to finish a lot of work to do"
    # The prefix belongs to an earlier, already-finalized fragment and must not
    # absorb this fragment's text, or the next decode would append to it again.
    assert handler._live_prefix_text == "yeah i still need to finish"


def test_progressive_redecode_replaces_fragment_instead_of_appending(monkeypatch):
    """Each progressive item re-decodes the whole current fragment.

    Parakeet flips words between passes, so appending each hypothesis repeated
    the sentence over and over in the caption. The newest decode must replace
    the fragment text, keeping only an earlier fragment's prefix in front.
    """
    handler = object.__new__(ParakeetTDTSTTHandler)
    handler._live_prefix_text = "so anyway"
    hypotheses = iter(
        [
            "i have a lot of testing a lot of dropping stuff",
            "i have a lot of testing a lot of jobing stuff but still",
            "i have a lot of testing a lot of dropping stuff but still like you know",
        ]
    )
    handler.streaming_handler = SimpleNamespace(
        transcribe_incremental=lambda audio: SimpleNamespace(
            fixed_text="",
            active_text=next(hypotheses),
        )
    )
    monkeypatch.setattr(parakeet_tdt_handler.console, "print", lambda *args, **kwargs: None)

    audio = np.zeros(16000, dtype=np.float32)
    for _ in range(3):
        caption = handler._show_progressive_transcription(audio)

    assert caption == "so anyway i have a lot of testing a lot of dropping stuff but still like you know"
    assert caption.count("i have a lot of testing") == 1


def test_same_turn_revision_keeps_live_caption_prefix():
    handler = object.__new__(ParakeetTDTSTTHandler)
    handler.enable_live_transcription = True
    handler._live_turn_key = ("turn_1", 0)
    handler._live_prefix_turn_id = "turn_1"
    handler._live_prefix_text = ""
    handler._live_final_turn_id = "turn_1"
    handler._live_final_text = "yeah i still need to finish"
    reset_calls = []
    handler.streaming_handler = SimpleNamespace(reset=lambda: reset_calls.append(True))

    handler._prepare_live_transcription_turn("turn_1", 1)
    assert reset_calls == [True]
    assert handler._live_prefix_text == "yeah i still need to finish"

    handler._prepare_live_transcription_turn("turn_2", 0)
    assert handler._live_prefix_text == ""
    assert handler._live_final_text == ""
    assert handler._live_prefix_turn_id == "turn_2"


def test_progressive_after_final_same_turn_stitches_locked_caption(monkeypatch):
    handler = object.__new__(ParakeetTDTSTTHandler)
    handler.enable_live_transcription = True
    handler.backend = "mlx"
    handler.last_language = "en"
    handler.start_language = None
    handler.processing_final = False
    handler.sample_rate = 16000
    handler.streaming_handler = SmartProgressiveStreamingHandler(object())
    handler.streaming_handler._decode_window = lambda audio: SimpleNamespace(
        text="a lot of work to do",
        sentences=[],
    )

    @contextmanager
    def fake_lock(*args, **kwargs):
        yield True

    handler._compute_lock_context = fake_lock
    handler._process_mlx_final = lambda audio_input: ("yeah i still need to finish", "en")
    monkeypatch.setattr(parakeet_tdt_handler.console, "print", lambda *args, **kwargs: None)

    list(
        handler.process(
            VADAudio(
                audio=np.zeros(16000, dtype=np.float32),
                mode="final",
                turn_id="turn_1",
                turn_revision=0,
            )
        )
    )
    result = list(
        handler.process(
            VADAudio(
                audio=np.zeros(16000, dtype=np.float32),
                mode="progressive",
                turn_id="turn_1",
                turn_revision=1,
            )
        )
    )

    assert len(result) == 1
    assert result[0].text == "yeah i still need to finish a lot of work to do"


def test_live_transcription_clears_terminal_line_before_each_update(monkeypatch):
    calls = []

    class FakeConsole:
        is_terminal = True
        width = 80

        def __init__(self):
            self.file = io.StringIO()

        def print(self, *args, **kwargs):
            calls.append((args, kwargs))

    handler = object.__new__(ParakeetTDTSTTHandler)
    handler._live_transcription_active = False
    fake_console = FakeConsole()
    monkeypatch.setattr(parakeet_tdt_handler, "console", fake_console)

    handler._print_live_transcription(Text("Live: first"), "first")
    handler._print_live_transcription(Text("Live: second"), "second")
    handler._clear_live_transcription_line()

    assert [args[0].plain for args, _ in calls] == ["Live: first", "Live: second"]
    assert [kwargs for _, kwargs in calls] == [{"end": ""}, {"end": ""}]
    assert fake_console.file.getvalue() == "\r\x1b[2K\r\r\x1b[2K\r\r\x1b[2K"
    assert handler._live_transcription_active is False


def test_live_transcription_truncates_terminal_updates(monkeypatch):
    calls = []

    class FakeConsole:
        is_terminal = True
        width = 14

        def __init__(self):
            self.file = io.StringIO()

        def print(self, *args, **kwargs):
            calls.append((args, kwargs))

    handler = object.__new__(ParakeetTDTSTTHandler)
    handler._live_transcription_active = False
    monkeypatch.setattr(parakeet_tdt_handler, "console", FakeConsole())

    handler._print_live_transcription(Text("Live: abcdefghijklmnopqrstuvwxyz"), "abcdefghijklmnopqrstuvwxyz")

    printed_text = calls[0][0][0]
    assert printed_text.plain == "Live: abcdef\u2026"
    assert len(printed_text.plain) == 13


def test_live_transcription_uses_lines_for_non_terminal_logs(monkeypatch):
    calls = []

    class FakeConsole:
        is_terminal = False

        def print(self, *args, **kwargs):
            calls.append((args, kwargs))

    handler = object.__new__(ParakeetTDTSTTHandler)
    handler._live_transcription_active = False
    monkeypatch.setattr(parakeet_tdt_handler, "console", FakeConsole())

    handler._print_live_transcription(Text("Live: first"), "first")

    assert calls == []
    assert handler._live_transcription_active is False


def test_process_yields_partial_tagged_tuple(monkeypatch):
    handler = object.__new__(ParakeetTDTSTTHandler)
    handler.enable_live_transcription = True
    handler.processing_final = False

    @contextmanager
    def fake_lock(*args, **kwargs):
        yield True

    handler._compute_lock_context = fake_lock
    handler._show_progressive_transcription = lambda audio: "partial text"
    monkeypatch.setattr(parakeet_tdt_handler.console, "print", lambda *args, **kwargs: None)

    result = list(handler.process(VADAudio(audio=np.zeros(16000, dtype=np.float32), mode="progressive")))

    assert len(result) == 1
    assert isinstance(result[0], PartialTranscription)
    assert result[0].text == "partial text"


def test_process_yields_final_transcript(monkeypatch):
    handler = object.__new__(ParakeetTDTSTTHandler)
    handler.enable_live_transcription = False
    handler.backend = "mlx"
    handler.last_language = "en"
    handler.start_language = None

    @contextmanager
    def fake_lock(*args, **kwargs):
        yield True

    handler._compute_lock_context = fake_lock
    handler._process_mlx_final = lambda audio_input: ("I am here.", "en")
    monkeypatch.setattr(parakeet_tdt_handler.console, "print", lambda *args, **kwargs: None)

    result = list(handler.process(VADAudio(audio=np.zeros(16000, dtype=np.float32))))

    assert len(result) == 1
    assert isinstance(result[0], Transcription)
    assert result[0].text == "I am here."
    assert result[0].language_code == "en"


def test_final_transcription_lock_timeout_sets_error(monkeypatch):
    handler = object.__new__(ParakeetTDTSTTHandler)
    handler.enable_live_transcription = False
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


def test_parakeet_timing_logs_only_final_transcriptions():
    handler = object.__new__(ParakeetTDTSTTHandler)
    handler._times = [0.01]

    assert handler.timing_log_level == logging.INFO
    assert handler.should_log_timing(Transcription(text="I am here.", language_code="en"))
    assert not handler.should_log_timing(PartialTranscription(text="I am"))


def test_final_transcription_resets_live_streaming_state(monkeypatch):
    handler = object.__new__(ParakeetTDTSTTHandler)
    handler.enable_live_transcription = True
    handler.backend = "mlx"
    handler.last_language = "en"
    handler.start_language = None
    handler.processing_final = False
    handler._live_turn_key = (None, None)
    reset_calls = []
    handler.streaming_handler = SimpleNamespace(reset=lambda: reset_calls.append(True))

    @contextmanager
    def fake_lock(*args, **kwargs):
        yield True

    handler._compute_lock_context = fake_lock
    handler._process_mlx_final = lambda audio_input: ("I am here.", "en")
    monkeypatch.setattr(parakeet_tdt_handler.console, "print", lambda *args, **kwargs: None)

    result = list(handler.process(VADAudio(audio=np.zeros(16000, dtype=np.float32), mode="final")))

    assert len(result) == 1
    assert isinstance(result[0], Transcription)
    assert handler.processing_final is False
    assert reset_calls == [True]


def test_turn_change_resets_live_streaming_state_before_progressive(monkeypatch):
    handler = object.__new__(ParakeetTDTSTTHandler)
    handler.enable_live_transcription = True
    handler.processing_final = False
    handler._live_turn_key = ("turn_1", 0)
    reset_calls = []
    handler.streaming_handler = SimpleNamespace(reset=lambda: reset_calls.append(True))

    @contextmanager
    def fake_lock(*args, **kwargs):
        yield True

    handler._compute_lock_context = fake_lock
    handler._show_progressive_transcription = lambda audio: "new partial"
    monkeypatch.setattr(parakeet_tdt_handler.console, "print", lambda *args, **kwargs: None)

    result = list(
        handler.process(
            VADAudio(
                audio=np.zeros(16000, dtype=np.float32),
                mode="progressive",
                turn_id="turn_2",
                turn_revision=0,
            )
        )
    )

    assert reset_calls == [True]
    assert len(result) == 1
    assert isinstance(result[0], PartialTranscription)
    assert result[0].text == "new partial"


def test_mlx_final_ignores_fixed_text_that_exceeds_current_audio(monkeypatch):
    handler = object.__new__(ParakeetTDTSTTHandler)
    handler.enable_live_transcription = True
    handler.backend = "mlx"
    handler.last_language = "en"
    handler.start_language = None
    handler.processing_final = False
    handler._live_turn_key = ("turn_3", 0)
    handler.streaming_handler = SimpleNamespace(
        fixed_sentences=["stale previous transcript"],
        fixed_end_time=10.0,
        reset=lambda: None,
    )

    @contextmanager
    def fake_lock(*args, **kwargs):
        yield True

    handler._compute_lock_context = fake_lock
    handler._process_mlx = lambda audio_input: ("new short turn", "en")
    monkeypatch.setattr(parakeet_tdt_handler.console, "print", lambda *args, **kwargs: None)

    result = list(
        handler.process(
            VADAudio(
                audio=np.zeros(16000, dtype=np.float32),
                mode="final",
                turn_id="turn_3",
                turn_revision=0,
                active_speech_ms=416,
            )
        )
    )

    assert len(result) == 1
    assert isinstance(result[0], Transcription)
    assert result[0].text == "new short turn"
    assert result[0].active_speech_ms == 416


def test_final_transcription_prevents_stale_fixed_window_on_next_progressive(monkeypatch):
    progressive_window_lengths = []
    handler = object.__new__(ParakeetTDTSTTHandler)
    handler.enable_live_transcription = True
    handler.backend = "mlx"
    handler.last_language = "en"
    handler.start_language = None
    handler.processing_final = False
    handler.streaming_handler = SmartProgressiveStreamingHandler(object())
    handler.streaming_handler._decode_window = lambda audio: (
        progressive_window_lengths.append(len(audio)) or SimpleNamespace(text="new partial", sentences=[])
    )
    handler.streaming_handler.fixed_sentences = ["previous fixed sentence"]
    handler.streaming_handler.fixed_end_time = 10.0
    handler.streaming_handler.last_transcribed_length = 20 * 16000

    @contextmanager
    def fake_lock(*args, **kwargs):
        yield True

    handler._compute_lock_context = fake_lock
    handler._process_mlx_final = lambda audio_input: ("previous final", "en")
    monkeypatch.setattr(parakeet_tdt_handler.console, "print", lambda *args, **kwargs: None)

    final_result = list(handler.process(VADAudio(audio=np.zeros(16000, dtype=np.float32), mode="final")))
    progressive_audio = np.zeros(852 * 16, dtype=np.float32)
    progressive_result = list(handler.process(VADAudio(audio=progressive_audio, mode="progressive")))

    assert len(final_result) == 1
    assert isinstance(final_result[0], Transcription)
    assert progressive_window_lengths == [len(progressive_audio)]
    assert len(progressive_result) == 1
    assert isinstance(progressive_result[0], PartialTranscription)
    assert progressive_result[0].text == "new partial"


def test_on_session_end_resets_streaming_state():
    handler = object.__new__(ParakeetTDTSTTHandler)
    handler.start_language = "en"
    handler.enable_live_transcription = True
    handler.processing_final = True
    reset_calls = []
    handler.streaming_handler = SimpleNamespace(reset=lambda: reset_calls.append(True))

    handler.on_session_end()

    assert handler.processing_final is False
    assert handler.last_language == "en"
    assert reset_calls == [True]


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


def test_process_mlx_decodes_long_audio_in_windows(monkeypatch):
    handler = object.__new__(ParakeetTDTSTTHandler)
    handler.sample_rate = 16_000
    handler.start_language = "en"
    handler.last_language = "en"
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
    text, language = handler._process_mlx(np.zeros(8, dtype=np.float32))
    assert language == "en"
    assert text == "part1 part2"
    assert seen == [3, 5]


def test_transcribe_incremental_decodes_equal_length_sliding_windows():
    calls = []
    handler = SmartProgressiveStreamingHandler(object())
    handler._decode_window = lambda audio: (
        calls.append(len(audio)) or SimpleNamespace(text=f"live-{len(calls)}", sentences=[])
    )
    window = np.zeros(16_000, dtype=np.float32)
    first = handler.transcribe_incremental(window)
    second = handler.transcribe_incremental(window)
    assert [first.active_text, second.active_text] == ["live-1", "live-2"]
    assert calls == [16_000, 16_000]
