"""Microsoft VibeVoice conversational TTS handler for Apple Silicon (mlx-audio)."""

from __future__ import annotations

import logging
import re
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

DEFAULT_MODEL = "mlx-community/VibeVoice-Realtime-0.5B-8bit"
# One of the voice caches bundled in the model repo (voices/*.safetensors).
DEFAULT_VOICE = "en-Emma_woman"
DEFAULT_MAX_TOKENS = 1024
DEFAULT_CFG_SCALE = 1.5
# A VibeVoice voice name pattern, used to guard against an invalid/session
# voice being passed to load_voice() (which would fail).
VOICE_LOOKS_VIBEVOICE = re.compile(r"^(en|de|fr|it|jp|kr|nl|pl|pt|sp)-[A-Za-z0-9]+_(man|woman)$")
PIPELINE_SR = 24000
MODEL_SR = 24000
BLOCK_SIZE = 512


class VibeVoiceTTSHandler(BaseHandler[TTSIn, TTSOut]):
    """Synthesize speech with Microsoft VibeVoice (streaming) via mlx-audio."""

    def setup(
        self,
        should_listen: Event,
        model_name: str = DEFAULT_MODEL,
        voice: str = DEFAULT_VOICE,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        cfg_scale: float = DEFAULT_CFG_SCALE,
        ddpm_steps: int | None = None,
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
        self.max_tokens = int(max_tokens)
        self.cfg_scale = float(cfg_scale)
        self.ddpm_steps = ddpm_steps
        self.gen_kwargs = gen_kwargs or {}
        self.model_name = model_name
        logger.info("Loading VibeVoice model %s with mlx-audio", self.model_name)
        try:
            from mlx_audio.tts.utils import load_model

            self.model = load_model(self.model_name)
        except ImportError as exc:
            raise ImportError("VibeVoice TTS requires mlx-audio and its TTS dependencies. Run uv sync.") from exc
        self.warmup()

    def warmup(self) -> None:
        logger.info("Warming up VibeVoice")
        try:
            for _ in self._process("Hello, this is a warmup."):
                pass
        except Exception as exc:  # startup remains usable when a warmup-only call fails
            logger.warning("VibeVoice warmup failed: %s", exc)

    def _process(self, text: str) -> Iterator[np.ndarray]:
        with MLXLockContext(handler_name="VibeVoiceTTS", timeout=10.0) as acquired:
            if not acquired:
                raise TimeoutError("Timed out waiting for MLX lock")
            import soxr

            gen_kwargs = dict(self.gen_kwargs)
            max_tokens = int(gen_kwargs.pop("max_tokens", self.max_tokens))
            cfg_scale = float(gen_kwargs.pop("cfg_scale", self.cfg_scale))
            ddpm_steps = gen_kwargs.pop("ddpm_steps", self.ddpm_steps)
            # Spectral denoiser: removes the faint hiss VibeVoice keeps *inside*
            # the voice (the elevated inter-harmonic noise floor a gate can't
            # touch). Causal, so it streams with ~one frame of latency.
            spectral_enabled = bool(gen_kwargs.pop("spectral_denoise", True))
            spectral_floor = float(gen_kwargs.pop("spectral_denoise_floor", 0.04))
            denoiser = SpectralDenoiser(enabled=spectral_enabled, floor=spectral_floor)
            # Gentle downward expander that hides the model's remaining noise
            # floor (hiss) during pauses, without touching the voice.
            noise_gate_enabled = bool(gen_kwargs.pop("noise_gate", True))
            noise_gate_threshold = float(gen_kwargs.pop("noise_gate_threshold", 0.010))
            gate = TTSNoiseGate(
                enabled=noise_gate_enabled,
                threshold=noise_gate_threshold,
            )
            # VibeVoice yields GenerationResult chunks as it decodes; the model
            # stops on EOS, so max_tokens is only a safety ceiling.
            generation = self.model.generate(
                text=text,
                voice=self.voice,
                max_tokens=max_tokens,
                cfg_scale=cfg_scale,
                ddpm_steps=ddpm_steps,
                verbose=False,
                **gen_kwargs,
            )
            # Resample to the pipeline output rate only if it differs from the
            # model's native rate (VibeVoice is 24 kHz; the pipeline now plays
            # 24 kHz, so this is a pass-through for the default model).
            use_resample = MODEL_SR != PIPELINE_SR
            if use_resample:
                resampler = soxr.ResampleStream(MODEL_SR, PIPELINE_SR, 1, dtype="float32")
            cancel_generation = self.cancel_scope.generation if self.cancel_scope else None
            leftover = np.array([], dtype=np.int16)
            total_samples = 0
            start = perf_counter()
            first = True
            for item in generation:
                if cancel_generation is not None and self.cancel_scope and self.cancel_scope.is_stale(cancel_generation):
                    logger.info("VibeVoice generation cancelled")
                    return
                audio = np.asarray(item.audio, dtype=np.float32)
                if first:
                    logger.info("VibeVoice TTFA %.2fs", perf_counter() - start)
                    first = False
                if not audio.size:
                    continue
                if use_resample:
                    pcm = resampler.resample_chunk(np.ascontiguousarray(audio), last=False)
                else:
                    pcm = audio
                pcm = denoiser.process(pcm)
                pcm = gate.process(pcm)
                pcm = np.clip(pcm * 32768, -32768, 32767).astype(np.int16)
                pcm = np.concatenate((leftover, pcm))
                complete = len(pcm) // BLOCK_SIZE * BLOCK_SIZE
                for offset in range(0, complete, BLOCK_SIZE):
                    yield pcm[offset : offset + BLOCK_SIZE]
                    total_samples += BLOCK_SIZE
                leftover = pcm[complete:]
            tail = (
                resampler.resample_chunk(np.zeros(0, dtype=np.float32), last=True)
                if use_resample
                else np.zeros(0, dtype=np.float32)
            )
            # Denoise the soxr tail, then flush the denoiser's held overlap window.
            tail_float = denoiser.process(np.asarray(tail, dtype=np.float32))
            tail_float = np.concatenate([tail_float, denoiser.flush()])
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
            logger.info("VibeVoice generated %.2fs of audio in %.2fs (RTF %.2f)", audio_s, elapsed, rtf)

    def _apply_session_voice(self, runtime_config: Any, response: Any) -> None:
        # Only switch to a voice the model actually ships (so load_voice never fails).
        voice: str | None = None
        if response and getattr(response, "audio", None) and response.audio.output and response.audio.output.voice:
            voice = str(response.audio.output.voice)
        if voice is None and runtime_config is not None:
            audio = runtime_config.session.audio
            output = audio.output if audio is not None else None
            if output is not None and output.voice:
                voice = str(output.voice)
        if voice and VOICE_LOOKS_VIBEVOICE.match(voice) and voice != self.voice:
            logger.info("VibeVoice voice set to %s", voice)
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
                    "VibeVoice produced almost no audio for a long prompt (%d chars, %d samples); possible early EOS",
                    len(text),
                    produced,
                )
        except Exception as exc:
            logger.error("VibeVoice generation failed: %s", exc, exc_info=True)
            self._failed_turn = turn_key
            drop_queued_tts_inputs(self.queue_in, tts_input.turn_id, tts_input.turn_revision)
            if self.text_output_queue is not None:
                self.text_output_queue.put(
                    ResponseFailedEvent(
                        message=f"VibeVoice generation failed: {exc}",
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
            logger.warning("VibeVoice cleanup failed: %s", exc)
