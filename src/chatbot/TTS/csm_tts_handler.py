"""Sesame CSM-1B conversational TTS handler for Apple Silicon (mlx-audio)."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from queue import Queue
from threading import Event
from time import perf_counter
from typing import Any

import numpy as np
from rich.console import Console

from chatbot.baseHandler import BaseHandler
from chatbot.pipeline.cancel_scope import CancelScope
from chatbot.pipeline.control import SESSION_END, is_control_message
from chatbot.pipeline.events import ResponseFailedEvent
from chatbot.pipeline.handler_types import TTSIn, TTSOut
from chatbot.pipeline.messages import AUDIO_RESPONSE_DONE, PIPELINE_END, EndOfResponse, TTSInput
from chatbot.pipeline.queue_types import TextEventItem
from chatbot.pipeline.speculative_turns import SpeculativeTurnTracker
from chatbot.TTS.tts_common import drop_queued_tts_inputs
from chatbot.utils.mlx_lock import MLXLockContext

logger = logging.getLogger(__name__)
console = Console()

DEFAULT_MODEL = "mlx-community/csm-1b-8bit"
DEFAULT_VOICE = "conversational_b"
CSM_VOICES = ("conversational_a", "conversational_b")
DEFAULT_TEMPERATURE = 0.55
# Conversational replies should not ramble for a minute. The 90s upstream
# default lets voice_match-style drift generate 20–50s for two sentences.
MAX_AUDIO_LENGTH_MS = 20_000
TOP_K = 50
PIPELINE_SR = 16000
MODEL_SR = 24000
BLOCK_SIZE = 512
STREAMING_INTERVAL_S = 0.5


def install_prompt_cache(model: Any) -> None:
    """Cache the speaker prompt and its audio tokens on a CSM model instance.

    Upstream ``generate()`` rebuilds the speaker context on every call: it
    re-reads/resamples the ~30s prompt WAV (``default_speaker_prompt``) and
    re-runs the full Mimi audio-token encode over ~375 frames
    (``_tokenize_audio``) before the backbone can start producing frames.
    The prompt is identical across generations for a given voice, so wrap
    the model *instance* (not the module) to compute each piece once.

    Only audio arrays originating from the cached speaker segments are
    tokenized once; anything else (e.g. caller-supplied ``ref_audio``) is
    passed through uncached so the cache cannot grow unboundedly.
    """
    segment_cache: dict[Any, Any] = {}
    token_cache: dict[tuple[int, bool], tuple[Any, Any]] = {}
    # Hold references to the cached audio arrays so their ``id()`` stays valid
    # for the lifetime of the cache (ids are only unique among live objects).
    keepalive: list[Any] = []
    cached_audio_ids: set[int] = set()

    original_prompt = model.default_speaker_prompt
    original_tokenize_audio = model._tokenize_audio

    def cached_default_speaker_prompt(voice: str, repo_id: str = "sesame/csm-1b") -> list[Any]:
        if voice not in segment_cache:
            segment = original_prompt(voice, repo_id)[0]
            segment_cache[voice] = segment
            keepalive.append(segment.audio)
            cached_audio_ids.add(id(segment.audio))
        return [segment_cache[voice]]

    def cached_tokenize_audio(audio: Any, add_eos: bool = True) -> tuple[Any, Any]:
        if id(audio) in cached_audio_ids:
            key = (id(audio), add_eos)
            cached = token_cache.get(key)
            if cached is None:
                cached = original_tokenize_audio(audio, add_eos=add_eos)
                token_cache[key] = cached
            return cached
        return original_tokenize_audio(audio, add_eos=add_eos)

    model.default_speaker_prompt = cached_default_speaker_prompt
    model._tokenize_audio = cached_tokenize_audio


class CsmTTSHandler(BaseHandler[TTSIn, TTSOut]):
    """Synthesize speech with Sesame CSM-1B (conversational TTS) via mlx-audio."""

    def setup(
        self,
        should_listen: Event,
        model_name: str = DEFAULT_MODEL,
        voice: str = DEFAULT_VOICE,
        temperature: float = DEFAULT_TEMPERATURE,
        stream: bool = True,
        gen_kwargs: dict[str, Any] | None = None,
        cancel_scope: CancelScope | None = None,
        speculative_turns: SpeculativeTurnTracker | None = None,
        text_output_queue: Queue[TextEventItem] | None = None,
    ) -> None:
        self.should_listen = should_listen
        self.cancel_scope = cancel_scope
        self.speculative_turns = speculative_turns
        self.text_output_queue = text_output_queue
        self._failed_turn: tuple[str | None, int | None] | None = None
        self.voice = voice
        self.temperature = float(temperature)
        self.stream = stream
        self.gen_kwargs = gen_kwargs or {}
        self.model_name = model_name
        logger.info("Loading CSM-1B model %s with mlx-audio", self.model_name)
        try:
            from mlx_audio.tts.utils import load_model

            self.model = load_model(self.model_name)
        except ImportError as exc:
            raise ImportError("CSM-1B requires mlx-audio and its TTS dependencies. Run uv sync.") from exc
        install_prompt_cache(self.model)
        self.warmup()

    def warmup(self) -> None:
        logger.info("Warming up CSM-1B")
        try:
            for _ in self._process("Hello, this is a warmup."):
                pass
        except Exception as exc:  # startup remains usable when a warmup-only call fails
            logger.warning("CSM-1B warmup failed: %s", exc)

    def _process(self, text: str) -> Iterator[np.ndarray]:
        with MLXLockContext(handler_name="CsmTTS", timeout=10.0) as acquired:
            if not acquired:
                raise TimeoutError("Timed out waiting for MLX lock")
            import soxr
            from mlx_lm.sample_utils import make_sampler

            # Pull length/interval knobs out of gen_kwargs so they can't collide
            # with the explicit kwargs below, and default to the handler constants.
            gen_kwargs = dict(self.gen_kwargs)
            max_audio_length_ms = float(gen_kwargs.pop("max_audio_length_ms", MAX_AUDIO_LENGTH_MS))
            streaming_interval = float(gen_kwargs.pop("streaming_interval", STREAMING_INTERVAL_S))
            # voice_match=True (mlx-audio default) prepends the 30s speaker
            # prompt transcript to every sentence. CSM then rambles 20–50s for
            # a two-line reply. Tokenize the prompt as context only.
            gen_kwargs.pop("voice_match", None)
            generation = self.model.generate(
                text=text,
                voice=self.voice,
                speaker=0,
                sampler=make_sampler(temp=self.temperature, top_k=TOP_K),
                stream=self.stream,
                streaming_interval=streaming_interval,
                max_audio_length_ms=max_audio_length_ms,
                voice_match=False,
                **gen_kwargs,
            )
            # Stateful streaming resampler: emit audio as CSM produces it (low
            # time-to-first-audio) while keeping filter state across chunks, so
            # chunk boundaries stay clean (no stutter). soxr is already a project
            # dependency.
            resampler = soxr.ResampleStream(MODEL_SR, PIPELINE_SR, 1, dtype="float32")
            cancel_generation = self.cancel_scope.generation if self.cancel_scope else None
            leftover = np.array([], dtype=np.int16)
            total_samples = 0
            start = perf_counter()
            first = True
            for item in generation:
                if cancel_generation is not None and self.cancel_scope and self.cancel_scope.is_stale(cancel_generation):
                    logger.info("CSM-1B generation cancelled")
                    return
                audio = np.asarray(item.audio, dtype=np.float32)
                if first:
                    logger.info("CSM-1B TTFA %.2fs", perf_counter() - start)
                    first = False
                if not audio.size:
                    continue
                pcm = resampler.resample_chunk(np.ascontiguousarray(audio), last=False)
                pcm = np.clip(pcm * 32768, -32768, 32767).astype(np.int16)
                pcm = np.concatenate((leftover, pcm))
                complete = len(pcm) // BLOCK_SIZE * BLOCK_SIZE
                for offset in range(0, complete, BLOCK_SIZE):
                    yield pcm[offset : offset + BLOCK_SIZE]
                    total_samples += BLOCK_SIZE
                leftover = pcm[complete:]
            tail = resampler.resample_chunk(np.zeros(0, dtype=np.float32), last=True)
            tail = np.clip(tail * 32768, -32768, 32767).astype(np.int16)
            pcm = np.concatenate((leftover, tail))
            complete = len(pcm) // BLOCK_SIZE * BLOCK_SIZE
            for offset in range(0, complete, BLOCK_SIZE):
                yield pcm[offset : offset + BLOCK_SIZE]
                total_samples += BLOCK_SIZE
            leftover = pcm[complete:]
            if leftover.size:
                yield np.pad(leftover, (0, BLOCK_SIZE - len(leftover)))
                total_samples += len(leftover)
            audio_s = total_samples / PIPELINE_SR
            elapsed = perf_counter() - start
            rtf = elapsed / audio_s if audio_s > 0 else 0.0
            logger.info("CSM-1B generated %.2fs of audio in %.2fs (RTF %.2f)", audio_s, elapsed, rtf)
            if audio_s >= max_audio_length_ms / 1000.0 - 0.5:
                logger.warning(
                    "CSM-1B generation likely hit the %.0fms length cap (produced %.2fs); the tail may be truncated",
                    max_audio_length_ms,
                    audio_s,
                )

    def _apply_session_voice(self, runtime_config: Any, response: Any) -> None:
        voice: str | None = None
        if response and getattr(response, "audio", None) and response.audio.output and response.audio.output.voice:
            voice = str(response.audio.output.voice)
        if voice is None and runtime_config is not None:
            audio = runtime_config.session.audio
            output = audio.output if audio is not None else None
            if output is not None and output.voice:
                voice = str(output.voice)
        if voice in CSM_VOICES and voice != self.voice:
            logger.info("CSM voice set to %s", voice)
            self.voice = voice

    def _coalesce(self, current: TTSInput) -> str:
        parts = [current.text.strip()] if current.text.strip() else []
        if not hasattr(self.queue_in, "mutex"):
            return " ".join(parts)
        with self.queue_in.mutex:
            while self.queue_in.queue:
                following = self.queue_in.queue[0]
                if (
                    is_control_message(following, SESSION_END.kind)
                    or (isinstance(following, bytes) and following == PIPELINE_END)
                    or isinstance(following, EndOfResponse)
                    or not isinstance(following, TTSInput)
                ):
                    break
                if (current.turn_id, current.turn_revision) != (following.turn_id, following.turn_revision):
                    break
                self.queue_in.queue.popleft()
                if following.text.strip():
                    parts.append(following.text.strip())
        return " ".join(parts)

    def process(self, tts_input: TTSIn) -> Iterator[TTSOut]:
        tracker = self.speculative_turns
        if isinstance(tts_input, EndOfResponse):
            self._failed_turn = None
            if not tracker or tracker.is_latest(tts_input.turn_id, tts_input.turn_revision):
                yield AUDIO_RESPONSE_DONE
            return
        turn_key = (tts_input.turn_id, tts_input.turn_revision)
        if getattr(self, "_failed_turn", None) == turn_key:
            return
        if tracker and not tracker.is_latest(tts_input.turn_id, tts_input.turn_revision):
            return
        if tracker:
            tracker.commit(tts_input.turn_id, tts_input.turn_revision)
        self._apply_session_voice(tts_input.runtime_config, tts_input.response)
        text = self._coalesce(tts_input)
        if not text:
            return
        console.print(f"[green]ASSISTANT: {text}")
        try:
            first = True
            produced = 0
            for audio in self._process(text):
                if first and tts_input.speech_stopped_at_s is not None:
                    logger.info("Speech stopped to first audio: %.3fs", perf_counter() - tts_input.speech_stopped_at_s)
                    first = False
                produced += int(getattr(audio, "size", len(audio)))
                yield audio
            if produced <= BLOCK_SIZE and len(text) > 80:
                logger.warning(
                    "CSM-1B produced almost no audio for a long prompt (%d chars, %d samples); possible early EOS",
                    len(text),
                    produced,
                )
        except Exception as exc:
            logger.error("CSM-1B generation failed: %s", exc, exc_info=True)
            self._failed_turn = turn_key
            drop_queued_tts_inputs(self.queue_in, tts_input.turn_id, tts_input.turn_revision)
            if self.text_output_queue is not None:
                self.text_output_queue.put(
                    ResponseFailedEvent(
                        message=f"CSM-1B generation failed: {exc}",
                        turn_id=tts_input.turn_id,
                        turn_revision=tts_input.turn_revision,
                    )
                )

    def cleanup(self) -> None:
        try:
            del self.model
            import mlx.core as mx

            mx.clear_cache()
        except Exception as exc:
            logger.warning("CSM-1B cleanup failed: %s", exc)
