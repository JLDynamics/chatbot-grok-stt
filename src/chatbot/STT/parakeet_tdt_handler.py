"""
Parakeet TDT Speech-to-Text Handler

Uses mlx-audio with mlx-community/parakeet-tdt-1.1b on Apple Silicon.

The 1.1B model transcribes English speech (lowercase alphabet).
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from time import perf_counter
from typing import Any, Iterator, Optional

import numpy as np
from rich.console import Console
from rich.text import Text

from chatbot.pipeline.handler_types import STTIn, STTOut
from chatbot.pipeline.messages import PartialTranscription, Transcription
from chatbot.STT.base_stt_handler import BaseSTTHandler
from chatbot.STT.smart_progressive_streaming import PartialTranscription as ProgressiveStreamPartial
from chatbot.utils.mlx_lock import MLXLockContext

try:
    from lingua import Language, LanguageDetectorBuilder

    LINGUA_AVAILABLE = True
except ImportError:
    LINGUA_AVAILABLE = False

logger = logging.getLogger(__name__)
console = Console()

# Parakeet TDT 1.1B is trained for English transcription.
SUPPORTED_LANGUAGES = [
    "en",
]

# Lingua uses "nb" (Bokmål) for Norwegian instead of "no"
_LINGUA_CODE_MAP = {"no": "nb"}

if LINGUA_AVAILABLE:
    _lingua_iso_to_code = {
        lang.iso_code_639_1.name.lower(): lang for lang in Language.all() if lang.iso_code_639_1 is not None
    }
    _lingua_languages = [
        _lingua_iso_to_code[_LINGUA_CODE_MAP.get(code, code)]
        for code in SUPPORTED_LANGUAGES
        if _LINGUA_CODE_MAP.get(code, code) in _lingua_iso_to_code
    ]

    def _build_lingua_detector():
        # Preloading can take multiple seconds on some hardware, including the
        # deployed server. Pay that cost during STT setup instead of on the first
        # user request, where it would look like slow STT.
        return LanguageDetectorBuilder.from_languages(*_lingua_languages).with_preloaded_language_models().build()


_lingua_detector = None


def _get_lingua_detector():
    global _lingua_detector
    if not LINGUA_AVAILABLE:
        return None
    if _lingua_detector is None:
        _lingua_detector = _build_lingua_detector()
    return _lingua_detector


class ParakeetTDTSTTHandler(BaseSTTHandler):
    """
    Handles Speech-to-Text using NVIDIA Parakeet TDT model.

    Parakeet TDT 1.1B is a ~1.1B-parameter English ASR model.
    """

    def setup(
        self,
        model_name: str = "mlx-community/parakeet-tdt-1.1b",
        language: Optional[str] = None,
        gen_kwargs: dict[str, Any] = {},
        enable_live_transcription: bool = False,
        live_transcription_update_interval: float = 0.5,
    ) -> None:
        """
        Initialize the Parakeet TDT model.

        Args:
            model_name: MLX model identifier.
            language: Target language code (optional, model auto-detects)
            gen_kwargs: Additional generation kwargs
        """
        self.gen_kwargs = gen_kwargs
        self.start_language = language
        self.last_language = language if language else "en"
        self.enable_live_transcription = enable_live_transcription
        self.live_transcription_update_interval = live_transcription_update_interval
        self.sample_rate = 16000
        if not model_name.startswith("mlx-community/"):
            raise ValueError("Parakeet must use an mlx-community model on macOS.")
        self.model_name = model_name
        self.backend = "mlx"
        logger.info("Loading Parakeet TDT model: %s via mlx-audio", model_name)
        self._setup_mlx(model_name)
        _get_lingua_detector()

        # Setup streaming handler if live transcription is enabled
        self.streaming_handler = None
        if self.enable_live_transcription:
            from chatbot.STT.smart_progressive_streaming import (
                SmartProgressiveStreamingHandler,
            )

            self.streaming_handler = SmartProgressiveStreamingHandler(
                self.model,
                emission_interval=self.live_transcription_update_interval,
                max_window_size=15.0,
                sentence_buffer=2.0,
            )
            self.processing_final = False  # Track if we're processing final audio
            logger.info(f"Live transcription enabled for Parakeet TDT ({self.backend})")
        self._live_transcription_active = False
        self._live_turn_key: tuple[str | None, int | None] | None = None

        self.warmup()

    def _setup_mlx(self, model_name: str) -> None:
        """Setup for Apple Silicon using mlx-audio."""
        try:
            from mlx_audio.stt.generate import load_model

            self.backend = "mlx"
            with MLXLockContext(handler_name="ParakeetLoad"):
                self.model = load_model(model_name)
            logger.info("MLX Audio Parakeet model loaded successfully")
        except ImportError as e:
            raise ImportError(
                "mlx-audio is required for Parakeet TDT on Apple Silicon. Install with: pip install mlx-audio"
            ) from e

    def warmup(self) -> None:
        """Warm up the model with a dummy input."""
        logger.info(f"Warming up {self.__class__.__name__}")

        # Create 1 second of silence at 16kHz
        dummy_audio = np.zeros(16000, dtype=np.float32)

        try:
            import mlx.core as mx

            with MLXLockContext(handler_name="ParakeetWarmup"):
                audio_mx = mx.array(dummy_audio, dtype=mx.float32)
                _ = self.model.decode_chunk(audio_mx, verbose=False)

            logger.info("Model warmed up and ready")
        except Exception as e:
            logger.warning(f"Warmup failed: {e}")

    def process(self, vad_audio: STTIn) -> Iterator[STTOut]:
        """
        Process audio and generate transcription.

        Yields:
            :class:`PartialTranscription` or :class:`Transcription`
        """
        process_start_s = perf_counter()
        is_progressive = vad_audio.mode == "progressive"
        audio_input = vad_audio.audio

        # Ensure audio is float32 numpy array
        if not isinstance(audio_input, np.ndarray):
            audio_input = np.array(audio_input, dtype=np.float32)
        else:
            audio_input = audio_input.astype(np.float32)
        audio_duration_s = len(audio_input) / getattr(self, "sample_rate", 16000)
        item_age_s = self._item_age_s(vad_audio)

        self._prepare_live_transcription_turn(vad_audio.turn_id, vad_audio.turn_revision)

        # Handle progressive updates: yield tagged partial for TranscriptionNotifier
        if self.enable_live_transcription and is_progressive:
            # Ignore progressive updates if we're already processing final audio
            if self.processing_final:
                logger.debug("Skipping stale progressive update (final audio already received)")
                return

            # Try to acquire lock with short timeout - skip if busy
            lock_scope_start_s = perf_counter()
            with self._compute_lock_context(handler_name="ParakeetSTT-Progressive", timeout=0.01) as acquired:
                if acquired:
                    try:
                        inference_start_s = perf_counter()
                        progressive_text = self._show_progressive_transcription(audio_input)
                        inference_s = perf_counter() - inference_start_s
                        if inference_s >= 0.25:
                            logger.info(
                                "Parakeet progressive STT timing turn=%s rev=%s audio=%.3fs age=%.3fs "
                                "lock_scope=%.3fs inference=%.3fs chars=%d",
                                vad_audio.turn_id,
                                vad_audio.turn_revision,
                                audio_duration_s,
                                item_age_s,
                                perf_counter() - lock_scope_start_s,
                                inference_s,
                                len(progressive_text),
                            )
                        if progressive_text:
                            yield PartialTranscription(
                                text=progressive_text,
                                turn_id=vad_audio.turn_id,
                                turn_revision=vad_audio.turn_revision,
                            )
                            return
                    except Exception as e:
                        logger.debug(f"Progressive transcription failed: {e}")
                else:
                    logger.debug("Skipping progressive update (compute busy)")
            return

        # Handle final transcription (send to LLM)
        logger.info(
            "Parakeet final STT start turn=%s rev=%s audio=%.3fs age=%.3fs",
            vad_audio.turn_id,
            vad_audio.turn_revision,
            audio_duration_s,
            item_age_s,
        )
        inference_s = 0.0
        lock_scope_s = 0.0
        stt_error: str | None = None
        try:
            if self.enable_live_transcription:
                # Mark that we're processing final audio (ignore stale progressive updates)
                self.processing_final = True

            # TTS often holds the MLX lock for longer than a first 5s try.
            # Retry once with a long wait so barge-in speech is not dropped.
            lock_scope_start_s = perf_counter()
            acquired = False
            for timeout_s in (5.0, 25.0):
                with self._compute_lock_context(handler_name="ParakeetSTT-Final", timeout=timeout_s) as got:
                    lock_scope_s = perf_counter() - lock_scope_start_s
                    if got:
                        acquired = True
                        inference_start_s = perf_counter()
                        pred_text, language_code = self._process_mlx_final(audio_input)
                        inference_s = perf_counter() - inference_start_s
                        lock_scope_s = perf_counter() - lock_scope_start_s
                        break
                logger.warning(
                    "Final STT waiting for MLX lock (timeout=%.0fs, turn=%s)",
                    timeout_s,
                    vad_audio.turn_id,
                )
            if not acquired:
                logger.error("Failed to acquire compute lock for final transcription")
                pred_text = ""
                language_code = self.last_language
                stt_error = "stt_lock_timeout"

            # Validate and update language
            if language_code and language_code in SUPPORTED_LANGUAGES:
                self.last_language = language_code
            else:
                language_code = self.last_language

        except Exception as e:
            logger.error(f"Parakeet TDT inference failed: {e}")
            pred_text = ""
            language_code = self.last_language
            stt_error = "stt_failed"

        total_s = perf_counter() - process_start_s
        logger.info(
            "Parakeet final STT done turn=%s rev=%s total=%.3fs lock_scope=%.3fs inference=%.3fs chars=%d",
            vad_audio.turn_id,
            vad_audio.turn_revision,
            total_s,
            lock_scope_s,
            inference_s,
            len(pred_text),
        )
        logger.debug("Finished Parakeet TDT inference")
        self._clear_live_transcription_line()
        if pred_text.strip():
            console.print(f"[yellow]USER: {pred_text.strip()}")
            if language_code:
                console.print(f"[dim]Language: {language_code}[/dim]")

        # Reset per-utterance live transcription state only after final STT
        # completes. The streaming handler carries fixed sentence timing within
        # an utterance, and stale timing must not leak into the next turn.
        if self.enable_live_transcription:
            self.processing_final = False
            self._reset_live_transcription_state(clear_turn=True)

        yield Transcription(
            text=pred_text,
            language_code=language_code,
            turn_id=vad_audio.turn_id,
            turn_revision=vad_audio.turn_revision,
            speech_stopped_at_s=vad_audio.created_at_s,
            error=stt_error,
        )

    @property
    def timing_log_level(self) -> int:
        return logging.INFO

    def should_log_timing(self, output: STTOut) -> bool:
        return isinstance(output, Transcription) and self.last_time > self.min_time_to_debug

    def _detect_language_from_text(self, text: str) -> Optional[str]:
        """
        Detect language from transcribed text using lingua-py.

        Args:
            text: Transcribed text string

        Returns:
            Detected language code or None if detection fails
        """
        if not LINGUA_AVAILABLE:
            logger.warning("lingua-py not available, cannot detect language from text")
            return None

        # Skip very short utterances where language ID is still too noisy.
        if not text or len(text.strip()) < 20:
            return None

        detector = _get_lingua_detector()
        if detector is None:
            return None
        detected = detector.detect_language_of(text)
        if detected is None:
            return None

        code = detected.iso_code_639_1.name.lower()
        # Map back lingua-specific codes to our supported codes
        code = {v: k for k, v in _LINGUA_CODE_MAP.items()}.get(code, code)
        if code not in SUPPORTED_LANGUAGES:
            return None
        return code

    @contextmanager
    def _compute_lock_context(self, handler_name: str, timeout: float) -> Iterator[bool]:
        with MLXLockContext(handler_name=handler_name, timeout=timeout) as acquired:
            yield acquired

    def _show_progressive_transcription(self, audio_input: np.ndarray) -> str:
        """Run progressive transcription, print to console, and return the text."""
        result = self.streaming_handler.transcribe_incremental(audio_input)
        rich_text = Text()
        if result.fixed_text:
            rich_text.append("Live: ", style="dim")
            rich_text.append(result.fixed_text, style="yellow")
            if result.active_text:
                rich_text.append(" ", style="dim")

        if result.active_text:
            if not result.fixed_text:
                rich_text.append("Live: ", style="dim")
            rich_text.append(result.active_text, style="cyan dim")

        progressive_text = self._build_progressive_text(result)
        if progressive_text:
            self._print_live_transcription(rich_text, progressive_text)

        return progressive_text

    def _print_live_transcription(self, rich_text: Text, progressive_text: str) -> None:
        is_terminal = bool(getattr(console, "is_terminal", False))
        if is_terminal:
            self._write_live_control("\r\x1b[2K")
            if rich_text:
                console.print(self._truncate_live_transcription(rich_text), end="")
            else:
                fallback = Text("Live: ", style="dim")
                fallback.append(progressive_text, style="cyan dim")
                console.print(self._truncate_live_transcription(fallback), end="")
            self._write_live_control("\r")
            self._live_transcription_active = True
            return

        if rich_text:
            console.print(rich_text)
        else:
            console.print(f"[dim]Live: [/dim]{progressive_text}")

    def _clear_live_transcription_line(self) -> None:
        if not getattr(self, "_live_transcription_active", False):
            return
        self._write_live_control("\r\x1b[2K")
        self._live_transcription_active = False

    def _write_live_control(self, sequence: str) -> None:
        file = getattr(console, "file", None)
        if file is None:
            return
        file.write(sequence)
        file.flush()

    def _truncate_live_transcription(self, text: Text) -> Text:
        text = text.copy()
        width = getattr(console, "width", 80)
        try:
            max_width = max(1, int(width) - 1)
        except (TypeError, ValueError):
            max_width = 79
        text.truncate(max_width, overflow="ellipsis")
        return text

    def _prepare_live_transcription_turn(self, turn_id: str | None, turn_revision: int | None) -> None:
        if not self.enable_live_transcription:
            return
        turn_key = (turn_id, turn_revision)
        if getattr(self, "_live_turn_key", None) == turn_key:
            return
        self._reset_live_transcription_state(clear_turn=False)
        self._live_turn_key = turn_key

    def _reset_live_transcription_state(self, clear_turn: bool) -> None:
        self._clear_live_transcription_line()
        streaming_handler = getattr(self, "streaming_handler", None)
        if streaming_handler is not None:
            streaming_handler.reset()
        if clear_turn:
            self._live_turn_key = None

    def _build_progressive_text(self, result: ProgressiveStreamPartial) -> str:
        parts = []
        if result.fixed_text:
            parts.append(result.fixed_text.strip())
        if result.active_text:
            parts.append(result.active_text.strip())
        return " ".join(part for part in parts if part).strip()

    def _process_mlx_final(self, audio_input: np.ndarray) -> tuple[str, str]:
        """Decode the full final utterance.

        Progressive captions are UI-only. Stitching their ``fixed_sentences``
        onto a tail decode was a common source of duplicated or dropped words.
        """
        if self.streaming_handler is not None:
            self._clear_live_transcription_line()
            self.streaming_handler.reset()
        return self._process_mlx(audio_input)

    def _process_mlx(self, audio_input: np.ndarray) -> tuple[str, str]:
        """Process audio using MLX backend."""
        import mlx.core as mx

        # Convert numpy array to mx.array
        audio_mx = mx.array(audio_input, dtype=mx.float32)

        # Call decode_chunk directly with the audio array
        result = self.model.decode_chunk(audio_mx, verbose=False)

        # Extract text from result
        if hasattr(result, "text"):
            pred_text = result.text.strip()
        else:
            pred_text = str(result).strip()

        # Determine language:
        # 1. Use fixed language if specified by user
        # 2. Try to detect from transcribed text using langdetect
        # 3. Fall back to last known language
        if self.start_language and self.start_language != "auto":
            language_code = self.start_language
        else:
            # Detect language from transcribed text
            detected_lang = self._detect_language_from_text(pred_text)
            if detected_lang:
                language_code = detected_lang
            else:
                language_code = self.last_language

        return pred_text, language_code

    def cleanup(self) -> None:
        """Clean up model resources."""
        logger.info(f"Cleaning up {self.__class__.__name__}")
        if hasattr(self, "model"):
            del self.model

    def on_session_end(self) -> None:
        super().on_session_end()
        self.last_language = self.start_language if self.start_language else "en"
        if self.enable_live_transcription:
            self.processing_final = False
            self._reset_live_transcription_state(clear_turn=True)
        logger.debug("Parakeet TDT session state reset")
