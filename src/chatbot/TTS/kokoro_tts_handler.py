"""Kokoro-82M conversational TTS handler for Apple Silicon (via mlx-audio).

Matches the official speech-to-speech Kokoro setup: ``mlx-community/Kokoro-82M-bf16``
on Apple Silicon, ``bm_fable`` (British male) by default, automatic language ->
Kokoro-lang-code -> voice mapping from the STT language.
"""

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
from chatbot.TTS.tts_common import SpectralDenoiser, TTSNoiseGate, drop_queued_tts_inputs
from chatbot.utils.mlx_lock import MLXLockContext

logger = logging.getLogger(__name__)
console = Console()

DEFAULT_MODEL = "mlx-community/Kokoro-82M-bf16"
# Kokoro voice naming: <lang><gender>_<name>. Default = British male.
DEFAULT_VOICE = "bm_fable"
DEFAULT_LANG_CODE = "b"
DEFAULT_SPEED = 1.0
PIPELINE_SR = 24000
BLOCK_SIZE = 512

WHISPER_LANGUAGE_TO_KOKORO_LANG = {
    "en": "b",
    "ja": "j",
    "zh": "z",
    "fr": "f",
    "es": "e",
    "it": "i",
    "pt": "p",
    "hi": "h",
    "de": "b",
    "nl": "b",
    "pl": "b",
    "ru": "b",
    "uk": "b",
}

KOKORO_LANG_DEFAULT_VOICES = {
    "a": "af_heart",
    "b": "bm_fable",
    "e": "ef_dora",
    "f": "ff_siwis",
    "h": "hf_alpha",
    "i": "if_sara",
    "j": "jf_alpha",
    "p": "pf_dora",
    "z": "zf_xiaobei",
}


class KokoroTTSHandler(BaseHandler[TTSIn, TTSOut]):
    """Synthesize speech with Kokoro-82M (via mlx-audio) on Apple Silicon."""

    def setup(
        self,
        should_listen: Event,
        model_name: str = DEFAULT_MODEL,
        voice: str = DEFAULT_VOICE,
        lang_code: str = DEFAULT_LANG_CODE,
        speed: float = DEFAULT_SPEED,
        blocksize: int = BLOCK_SIZE,
        gen_kwargs: dict[str, Any] | None = None,
        cancel_scope: CancelScope | None = None,
        speculative_turns: SpeculativeTurnTracker | None = None,
        text_output_queue: Queue[TextEventItem] | None = None,
    ) -> None:
        self.should_listen = should_listen
        self.voice = voice
        self.lang_code = lang_code
        self.speed = float(speed)
        self.blocksize = int(blocksize)
        self.gen_kwargs = gen_kwargs or {}
        self.cancel_scope = cancel_scope
        self.speculative_turns = speculative_turns
        self.text_output_queue = text_output_queue
        self._failed_turn: tuple[str | None, int | None] | None = None
        self.model_name = model_name
        logger.info("Loading Kokoro model %s with mlx-audio", self.model_name)
        try:
            from mlx_audio.tts.utils import load_model

            self.model = load_model(self.model_name)
        except ImportError as exc:
            raise ImportError(
                "Kokoro TTS on Apple Silicon requires mlx-audio plus its TTS deps "
                "(misaki, espeakng-loader, num2words, spacy, phonemizer-fork). Run uv sync."
            ) from exc
        self.warmup()

    def warmup(self) -> None:
        logger.info("Warming up Kokoro")
        try:
            for _ in self._process("Hello, this is a warmup."):
                pass
        except Exception as exc:  # startup remains usable when a warmup-only call fails
            logger.warning("Kokoro warmup failed: %s", exc)

    def _process(self, text: str) -> Iterator[np.ndarray]:
        with MLXLockContext(handler_name="KokoroTTS", timeout=10.0) as acquired:
            if not acquired:
                raise TimeoutError("Timed out waiting for MLX lock")

            gen_kwargs = dict(self.gen_kwargs)
            speed = float(gen_kwargs.pop("speed", self.speed))
            lang_code = gen_kwargs.pop("lang_code", self.lang_code)
            # Spectral denoise (removes hiss inside the voice) + noise gate
            # (removes pause hiss), shared with the VibeVoice backend.
            spectral_enabled = bool(gen_kwargs.pop("spectral_denoise", True))
            spectral_floor = float(gen_kwargs.pop("spectral_denoise_floor", 0.04))
            denoiser = SpectralDenoiser(enabled=spectral_enabled, floor=spectral_floor)
            noise_gate_enabled = bool(gen_kwargs.pop("noise_gate", True))
            noise_gate_threshold = float(gen_kwargs.pop("noise_gate_threshold", 0.010))
            gate = TTSNoiseGate(enabled=noise_gate_enabled, threshold=noise_gate_threshold)

            generation = self.model.generate(
                text=text,
                voice=self.voice,
                speed=speed,
                lang_code=lang_code,
                **gen_kwargs,
            )
            cancel_generation = self.cancel_scope.generation if self.cancel_scope else None
            leftover = np.array([], dtype=np.int16)
            total_samples = 0
            start = perf_counter()
            first = True
            for item in generation:
                if cancel_generation is not None and self.cancel_scope and self.cancel_scope.is_stale(cancel_generation):
                    logger.info("Kokoro generation cancelled")
                    return
                audio = np.asarray(item.audio, dtype=np.float32)
                if first:
                    logger.info("Kokoro TTFA %.2fs", perf_counter() - start)
                    first = False
                if not audio.size:
                    continue
                pcm = denoiser.process(audio)
                pcm = gate.process(pcm)
                pcm = np.clip(pcm * 32768, -32768, 32767).astype(np.int16)
                pcm = np.concatenate((leftover, pcm))
                complete = len(pcm) // BLOCK_SIZE * BLOCK_SIZE
                for offset in range(0, complete, BLOCK_SIZE):
                    yield pcm[offset : offset + BLOCK_SIZE]
                    total_samples += BLOCK_SIZE
                leftover = pcm[complete:]
            tail_float = denoiser.flush()
            tail_float = gate.process(tail_float)
            tail = np.clip(tail_float * 32768, -32768, 32767).astype(np.int16)
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
            logger.info("Kokoro generated %.2fs of audio in %.2fs (RTF %.2f)", audio_s, elapsed, rtf)

    def _apply_language(self, tts_input: TTSInput) -> None:
        # Map the STT-detected language to a Kokoro lang code + native voice.
        lang = (tts_input.language_code or "").lower()
        if not lang:
            return
        kokoro_lang = WHISPER_LANGUAGE_TO_KOKORO_LANG.get(lang)
        if kokoro_lang and kokoro_lang != self.lang_code:
            new_voice = KOKORO_LANG_DEFAULT_VOICES.get(kokoro_lang, self.voice)
            logger.info("Kokoro language %s -> %s, voice %s -> %s", lang, kokoro_lang, self.voice, new_voice)
            self.lang_code = kokoro_lang
            self.voice = new_voice

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
        self._apply_language(tts_input)
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
                    "Kokoro produced almost no audio for a long prompt (%d chars, %d samples); possible early EOS",
                    len(text),
                    produced,
                )
        except Exception as exc:
            logger.error("Kokoro generation failed: %s", exc, exc_info=True)
            self._failed_turn = turn_key
            drop_queued_tts_inputs(self.queue_in, tts_input.turn_id, tts_input.turn_revision)
            if self.text_output_queue is not None:
                self.text_output_queue.put(
                    ResponseFailedEvent(
                        message=f"Kokoro generation failed: {exc}",
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
            logger.warning("Kokoro cleanup failed: %s", exc)
