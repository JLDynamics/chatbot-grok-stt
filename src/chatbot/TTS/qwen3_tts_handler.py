"""Streaming Qwen3-TTS CustomVoice handler for Apple Silicon."""

from __future__ import annotations

import logging
import math
import re
import unicodedata
from collections.abc import Iterator
from threading import Event
from time import perf_counter
from typing import Any

import numpy as np
from openai.types.realtime.realtime_response_create_params import RealtimeResponseCreateParams
from rich.console import Console

from chatbot.api.openai_realtime.runtime_config import RuntimeConfig
from chatbot.baseHandler import BaseHandler
from chatbot.pipeline.cancel_scope import CancelScope
from chatbot.pipeline.control import SESSION_END, is_control_message
from chatbot.pipeline.handler_types import TTSIn, TTSOut
from chatbot.pipeline.messages import AUDIO_RESPONSE_DONE, PIPELINE_END, EndOfResponse, TTSInput
from chatbot.pipeline.speculative_turns import SpeculativeTurnTracker
from chatbot.utils.mlx_lock import MLXLockContext

logger = logging.getLogger(__name__)
console = Console()

DEFAULT_MODEL = "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice"
DEFAULT_MLX_MODEL = "mlx-community/Qwen3-TTS-12Hz-1.7B-CustomVoice-8bit"
VALID_MLX_QUANTIZATIONS = ("bf16", "4bit", "6bit", "8bit")
PIPELINE_SR = 16000
MODEL_TOKENS_PER_SECOND = 12.5
MIN_UTTERANCE_TOKENS = 360
MAX_UTTERANCE_TOKENS = 1536
STREAMING_INTERVAL_S = 0.32
BLOCK_SIZE = 512
CJK_PATTERN = re.compile(
    r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\uf900-\ufaff"
    r"\U00020000-\U0002fa1f]"
)
LANGUAGE_ALIASES = {
    "zh": "chinese",
    "en": "english",
    "ja": "japanese",
    "ko": "korean",
    "de": "german",
    "fr": "french",
    "ru": "russian",
    "pt": "portuguese",
    "es": "spanish",
    "it": "italian",
}


class Qwen3TTSHandler(BaseHandler[TTSIn, TTSOut]):
    """Synthesize built-in Qwen CustomVoice speakers with mlx-audio."""

    def setup(
        self,
        should_listen: Event,
        model_name: str = DEFAULT_MODEL,
        temperature: float = 0.5,
        speaker: str = "Ryan",
        mlx_quantization: str = "8bit",
        language: str = "auto",
        gen_kwargs: dict[str, Any] | None = None,
        cancel_scope: CancelScope | None = None,
        speculative_turns: SpeculativeTurnTracker | None = None,
    ) -> None:
        self.should_listen = should_listen
        self.cancel_scope = cancel_scope
        self.speculative_turns = speculative_turns
        self.temperature = float(temperature)
        self.speaker = speaker
        self._initial_speaker = speaker
        self.language = self._normalize_language(language)
        self.gen_kwargs = gen_kwargs or {}
        self.mlx_quantization = self._normalize_quantization(mlx_quantization)
        self.model_name = self._resolve_model_name(model_name)
        logger.info("Loading Qwen3-TTS model %s with mlx-audio", self.model_name)
        try:
            from mlx_audio.tts.utils import load_model

            self.model = load_model(self.model_name)
        except ImportError as exc:
            raise ImportError("Qwen3-TTS requires mlx-audio and its TTS dependencies. Run uv sync.") from exc
        self.warmup()

    @staticmethod
    def _normalize_quantization(value: str) -> str:
        normalized = str(value).strip().lower()
        if normalized not in VALID_MLX_QUANTIZATIONS:
            choices = ", ".join(VALID_MLX_QUANTIZATIONS)
            raise ValueError(f"Unsupported MLX quantization {value!r}; choose {choices}.")
        return normalized

    def _resolve_model_name(self, model_name: str) -> str:
        name = model_name or DEFAULT_MODEL
        if name.startswith("Qwen/"):
            name = name.replace("Qwen/", "mlx-community/", 1)
        if not name.startswith("mlx-community/"):
            raise ValueError("Qwen3-TTS must use an mlx-community CustomVoice model on macOS.")
        if "CustomVoice" not in name:
            raise ValueError("Only the Qwen3-TTS 1.7B CustomVoice model is supported.")
        for suffix in VALID_MLX_QUANTIZATIONS:
            marker = f"-{suffix}"
            if name.endswith(marker):
                return name[: -len(marker)] + f"-{self.mlx_quantization}"
        return f"{name}-{self.mlx_quantization}"

    @staticmethod
    def _normalize_language(language: str | None) -> str:
        normalized = str(language or "auto").strip().replace("_", "-").lower()
        return LANGUAGE_ALIASES.get(normalized, normalized or "auto")

    def _supported_speakers(self) -> list[str] | None:
        getter = getattr(self.model, "get_supported_speakers", None)
        if not callable(getter):
            return None
        speakers = getter()
        return [str(value) for value in speakers] if speakers is not None else None

    def _resolve_speaker(self) -> str:
        supported = self._supported_speakers()
        if supported:
            by_lower = {value.lower(): value for value in supported}
            resolved = by_lower.get(self.speaker.lower())
            if resolved is None:
                raise ValueError(f"Unsupported Qwen voice {self.speaker!r}; choose {', '.join(sorted(supported))}.")
            return resolved
        return self.speaker

    def _apply_session_voice_override(
        self,
        runtime_config: RuntimeConfig | None,
        response: RealtimeResponseCreateParams | None,
    ) -> None:
        voice: str | None = None
        if response and response.audio and response.audio.output and response.audio.output.voice:
            voice = str(response.audio.output.voice)
        if voice is None and runtime_config is not None:
            audio = runtime_config.session.audio
            output = audio.output if audio is not None else None
            if output is not None and output.voice:
                voice = str(output.voice)
        if voice is None:
            return
        supported = self._supported_speakers()
        if supported:
            by_lower = {value.lower(): value for value in supported}
            if voice.lower() not in by_lower:
                logger.warning("Ignoring unsupported Qwen session voice %r", voice)
                return
            voice = by_lower[voice.lower()]
        self.speaker = voice

    def warmup(self) -> None:
        logger.info("Warming up Qwen3-TTS")
        try:
            for _ in self._process_custom_voice("Hello, this is a warmup."):
                pass
        except Exception as exc:  # startup remains usable when a warmup-only call fails
            logger.warning("Qwen3-TTS warmup failed: %s", exc)

    def _estimate_max_tokens(self, text: str) -> int:
        words = len(re.findall(r"\w+", text, flags=re.UNICODE))
        chars = len(re.sub(r"\s+", "", text))
        cjk = len(CJK_PATTERN.findall(text))
        seconds = max(words / 2.6 if words else 0, chars / 14 if chars else 0, cjk / 5.5 if cjk else 0)
        seconds += sum(unicodedata.category(ch).startswith("P") for ch in text) * 0.5 + 1.0
        tokens = math.ceil(seconds * MODEL_TOKENS_PER_SECOND * 1.35)
        return min(MAX_UTTERANCE_TOKENS, max(MIN_UTTERANCE_TOKENS, tokens))

    @staticmethod
    def _prepare_audio_chunk(item: Any) -> tuple[np.ndarray | None, int | None]:
        if isinstance(item, tuple):
            audio, sample_rate, _timing = item
            return np.asarray(audio, dtype=np.float32), int(sample_rate)
        audio = getattr(item, "audio", None)
        if audio is None:
            return None, None
        return np.asarray(audio, dtype=np.float32).squeeze(), int(getattr(item, "sample_rate", PIPELINE_SR))

    @staticmethod
    def _resample(audio: np.ndarray, sample_rate: int) -> np.ndarray:
        if sample_rate == PIPELINE_SR:
            return audio
        from scipy.signal import resample_poly

        divisor = math.gcd(PIPELINE_SR, sample_rate)
        return resample_poly(audio, PIPELINE_SR // divisor, sample_rate // divisor)

    def _stream(self, generation: Any) -> Iterator[np.ndarray]:
        cancel_generation = self.cancel_scope.generation if self.cancel_scope else None
        start = perf_counter()
        total_samples = 0
        found_speech = False
        leftover = np.array([], dtype=np.int16)
        first = True
        for item in generation:
            if cancel_generation is not None and self.cancel_scope and self.cancel_scope.is_stale(cancel_generation):
                logger.info("Qwen3-TTS generation cancelled")
                return
            audio, sample_rate = self._prepare_audio_chunk(item)
            if audio is None or sample_rate is None or not audio.size:
                continue
            if first:
                logger.info("Qwen3-TTS TTFA %.2fs", perf_counter() - start)
                first = False
            audio = np.clip(self._resample(audio, sample_rate) * 32768, -32768, 32767).astype(np.int16)
            if not found_speech:
                above = np.abs(audio) > int(32768 * 0.01)
                if not np.any(above):
                    continue
                audio = audio[max(0, int(np.argmax(above)) - int(PIPELINE_SR * 0.04)) :]
                found_speech = True
            audio = np.concatenate((leftover, audio))
            complete = len(audio) // BLOCK_SIZE * BLOCK_SIZE
            for offset in range(0, complete, BLOCK_SIZE):
                yield audio[offset : offset + BLOCK_SIZE]
                total_samples += BLOCK_SIZE
            leftover = audio[complete:]
        if leftover.size:
            yield np.pad(leftover, (0, BLOCK_SIZE - len(leftover)))
            total_samples += len(leftover)
        elapsed = perf_counter() - start
        logger.info("Qwen3-TTS generated %.2fs of audio in %.2fs", total_samples / PIPELINE_SR, elapsed)

    def _process_custom_voice(self, text: str) -> Iterator[np.ndarray]:
        with MLXLockContext(handler_name="Qwen3TTS", timeout=10.0) as acquired:
            if not acquired:
                raise TimeoutError("Timed out waiting for MLX lock")
            generation = self.model.generate_custom_voice(
                text=text,
                speaker=self._resolve_speaker(),
                language=self.language,
                temperature=self.temperature,
                max_tokens=self._estimate_max_tokens(text),
                verbose=False,
                stream=True,
                streaming_interval=STREAMING_INTERVAL_S,
                **self.gen_kwargs,
            )
            yield from self._stream(generation)

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
            if not tracker or tracker.is_latest_after_reopen_grace(tts_input.turn_id, tts_input.turn_revision):
                yield AUDIO_RESPONSE_DONE
            return
        if tracker and not tracker.is_latest_after_reopen_grace(tts_input.turn_id, tts_input.turn_revision):
            return
        if tracker:
            tracker.commit(tts_input.turn_id, tts_input.turn_revision)
        self._apply_session_voice_override(tts_input.runtime_config, tts_input.response)
        text = self._coalesce(tts_input) or "Hello."
        console.print(f"[green]ASSISTANT: {text}")
        try:
            first = True
            for audio in self._process_custom_voice(text):
                if first and tts_input.speech_stopped_at_s is not None:
                    logger.info("Speech stopped to first audio: %.3fs", perf_counter() - tts_input.speech_stopped_at_s)
                    first = False
                yield audio
        except Exception as exc:
            logger.error("Qwen3-TTS generation failed: %s", exc, exc_info=True)

    def on_session_end(self) -> None:
        self.speaker = self._initial_speaker

    def cleanup(self) -> None:
        try:
            del self.model
            import mlx.core as mx

            mx.clear_cache()
        except Exception as exc:
            logger.warning("Qwen3-TTS cleanup failed: %s", exc)
