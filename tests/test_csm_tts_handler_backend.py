from types import SimpleNamespace
from queue import Queue

import numpy as np

from chatbot.arguments_classes.csm_tts_arguments import CsmTTSHandlerArguments
from chatbot.backend_registry import TTS_BACKENDS, select_backend
from chatbot.pipeline.events import ResponseFailedEvent
from chatbot.pipeline.messages import TTSInput
from chatbot.TTS.csm_tts_handler import (
    BLOCK_SIZE,
    DEFAULT_MODEL,
    DEFAULT_VOICE,
    MODEL_SR,
    CsmTTSHandler,
    install_prompt_cache,
)


class _FakeModel:
    """Yields synthetic 24 kHz float chunks like mlx-audio's CSM Model.generate."""

    def __init__(self, chunks):
        self._chunks = chunks

    def generate(self, **kwargs):
        yield from self._chunks


def _chunk(samples: int, value: float = 0.1):
    return SimpleNamespace(audio=np.full(samples, value, dtype=np.float32), sample_rate=MODEL_SR)


def _handler_with_fake_model():
    handler = CsmTTSHandler.__new__(CsmTTSHandler)
    handler.voice = DEFAULT_VOICE
    handler.temperature = 0.8
    handler.stream = True
    handler.gen_kwargs = {}
    handler.cancel_scope = None
    return handler


class _CacheProbeModel:
    """Counts prompt builds / audio tokenizations like mlx-audio's CSM Model."""

    def __init__(self):
        self.prompt_calls = 0
        self.tokenize_audio_calls = 0
        self._segment = SimpleNamespace(
            speaker=0, text="prompt text", audio=object()
        )

    def default_speaker_prompt(self, voice, repo_id="sesame/csm-1b"):
        self.prompt_calls += 1
        return [self._segment]

    def _tokenize_audio(self, audio, add_eos=True):
        self.tokenize_audio_calls += 1
        return ("tokens", audio, add_eos)


def test_install_prompt_cache_reuses_prompt_and_audio_tokens():
    model = _CacheProbeModel()
    install_prompt_cache(model)

    first = model.default_speaker_prompt(DEFAULT_VOICE)
    second = model.default_speaker_prompt(DEFAULT_VOICE)
    assert model.prompt_calls == 1
    assert first[0] is second[0] is model._segment

    # Same audio object + same add_eos -> tokenized once.
    assert model._tokenize_audio(model._segment.audio, add_eos=False) == (
        "tokens", model._segment.audio, False
    )
    assert model._tokenize_audio(model._segment.audio, add_eos=False) == (
        "tokens", model._segment.audio, False
    )
    assert model.tokenize_audio_calls == 1

    # Different add_eos is a distinct cache entry.
    model._tokenize_audio(model._segment.audio, add_eos=True)
    assert model.tokenize_audio_calls == 2


def test_install_prompt_cache_passes_through_untracked_audio():
    model = _CacheProbeModel()
    install_prompt_cache(model)
    model.default_speaker_prompt(DEFAULT_VOICE)  # populate the segment cache

    foreign = object()
    model._tokenize_audio(foreign, add_eos=False)
    model._tokenize_audio(foreign, add_eos=False)
    # Non-prompt audio is never cached: one call per invocation.
    assert model.tokenize_audio_calls == 2


def test_empty_text_skips_synthesis():
    handler = _handler_with_fake_model()
    handler.text_output_queue = Queue()
    handler.speculative_turns = None
    handler.queue_in = Queue()
    handler._failed_turn = None
    handler.model = _FakeModel([_chunk(2400)])
    assert list(handler.process(TTSInput(text="   "))) == []


def test_generation_failure_drops_remaining_turn_inputs():
    handler = _handler_with_fake_model()
    handler.text_output_queue = Queue()
    handler.speculative_turns = None
    handler.queue_in = Queue()
    handler._failed_turn = None

    class _Boom:
        def generate(self, **kwargs):
            raise RuntimeError("boom")
            yield  # pragma: no cover

    handler.model = _Boom()
    handler.queue_in.put(TTSInput(text="more", turn_id="turn-1", turn_revision=2))
    list(handler.process(TTSInput(text="hello", turn_id="turn-1", turn_revision=2)))
    assert handler.queue_in.empty()
    assert handler._failed_turn == ("turn-1", 2)
    assert list(handler.process(TTSInput(text="again", turn_id="turn-1", turn_revision=2))) == []


def test_generation_failure_emits_response_failed_event():
    handler = _handler_with_fake_model()
    handler.text_output_queue = Queue()
    handler.speculative_turns = None
    handler.queue_in = Queue()

    class _Boom:
        def generate(self, **kwargs):
            raise RuntimeError("boom")
            yield  # pragma: no cover

    handler.model = _Boom()

    tts_input = TTSInput(text="hello", turn_id="turn-1", turn_revision=2)
    outputs = list(handler.process(tts_input))
    assert outputs == []
    event = handler.text_output_queue.get_nowait()
    assert isinstance(event, ResponseFailedEvent)
    assert event.turn_id == "turn-1"
    assert event.turn_revision == 2
    assert "boom" in event.message


def test_csm_backend_is_registered_with_normalized_config():
    selection = select_backend(TTS_BACKENDS, "csm", CsmTTSHandlerArguments())
    assert selection.kind == "tts"
    assert selection.config["model_name"] == DEFAULT_MODEL == "mlx-community/csm-1b-8bit"
    assert selection.config["voice"] == DEFAULT_VOICE == "conversational_b"
    assert selection.config["temperature"] == 0.55
    assert selection.config["stream"] is True
    assert selection.config["gen_kwargs"] == {
        "max_audio_length_ms": 20000.0,
        "streaming_interval": 0.5,
    }


def test_streaming_resample_emits_pcm16_blocks():
    handler = _handler_with_fake_model()
    # Three 0.1 s chunks @ 24 kHz = 0.3 s -> 4800 samples @ 16 kHz.
    handler.model = _FakeModel([_chunk(2400), _chunk(2400), _chunk(2400)])
    blocks = list(handler._process("hello"))
    assert blocks
    assert all(block.dtype == np.int16 for block in blocks)
    assert all(len(block) == BLOCK_SIZE for block in blocks)
    # Resampled audio plus the final 512-sample padding.
    assert sum(len(block) for block in blocks) >= 4800
