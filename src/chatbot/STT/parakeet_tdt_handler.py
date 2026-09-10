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

from chatbot.pipeline.handler_types import STTIn, STTOut
from chatbot.pipeline.messages import Transcription
from chatbot.STT.base_stt_handler import BaseSTTHandler
from chatbot.utils.mlx_lock import MLXLockContext

try:
    from lingua import Language, LanguageDetectorBuilder

    LINGUA_AVAILABLE = True
except ImportError:
    LINGUA_AVAILABLE = False

logger = logging.getLogger(__name__)
console = Console()

# One-shot decode of a 10-minute monologue is what pushed RAM into the
# multi-gigabyte range. 30s windows keep activations bounded.
_MAX_FINAL_DECODE_S = 30.0
# A hard cut lands mid-word and neither decode recovers it, so every boundary
# ate a word. Overlapping the windows instead would need the two decodes to be
# stitched, and independent Parakeet passes disagree on words often enough that
# stitching duplicates whole phrases. Nudge the boundary into the quietest
# nearby frame instead: windows stay gap-free and non-overlapping, so the
# decodes simply concatenate.
_BOUNDARY_SEARCH_S = 1.5
_BOUNDARY_FRAME_MS = 30


def _quiet_boundary(
    audio: np.ndarray,
    start: int,
    target_end: int,
    sample_rate: int,
) -> int:
    """Move a window boundary back to the quietest frame near ``target_end``."""
    frame = max(1, int(_BOUNDARY_FRAME_MS * sample_rate / 1000))
    earliest = max(start + frame, target_end - int(_BOUNDARY_SEARCH_S * sample_rate))
    if earliest + frame > target_end:
        return target_end
    best_end, best_energy = target_end, None
    for pos in range(earliest, target_end - frame + 1, frame):
        window = audio[pos : pos + frame]
        energy = float(np.dot(window, window))
        if best_energy is None or energy < best_energy:
            best_energy, best_end = energy, pos + frame // 2
    return best_end


def iter_decode_windows(
    audio: np.ndarray,
    sample_rate: int,
    max_s: float = _MAX_FINAL_DECODE_S,
) -> list[np.ndarray]:
    """Split long audio so each Parakeet decode stays bounded.

    Boundaries land in the quietest frame near each cut, so words are not split
    across two decodes. Windows are contiguous and non-overlapping: joining
    their transcripts with a space reconstructs the utterance.
    """
    if sample_rate <= 0:
        raise ValueError("sample_rate must be positive")
    max_samples = max(1, int(max_s * sample_rate))
    if len(audio) <= max_samples:
        return [audio]
    windows = []
    start = 0
    while start < len(audio):
        target_end = start + max_samples
        if target_end >= len(audio):
            windows.append(audio[start:])
            break
        end = _quiet_boundary(audio, start, target_end, sample_rate)
        windows.append(audio[start:end])
        start = end
    return windows


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
        self.sample_rate = 16000
        if not model_name.startswith("mlx-community/"):
            raise ValueError("Parakeet must use an mlx-community model on macOS.")
        self.model_name = model_name
        self.backend = "mlx"
        logger.info("Loading Parakeet TDT model: %s via mlx-audio", model_name)
        self._setup_mlx(model_name)
        _get_lingua_detector()
        self._reset_turn_decode_cache()
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
            :class:`Transcription`
        """
        process_start_s = perf_counter()
        audio_input = vad_audio.audio

        # Ensure audio is float32 numpy array
        if not isinstance(audio_input, np.ndarray):
            audio_input = np.array(audio_input, dtype=np.float32)
        else:
            audio_input = audio_input.astype(np.float32)
        audio_duration_s = len(audio_input) / getattr(self, "sample_rate", 16000)
        item_age_s = self._item_age_s(vad_audio)

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
                        pred_text, language_code = self._transcribe_turn(audio_input, vad_audio.turn_id)
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
        if pred_text.strip():
            console.print(f"[yellow]USER: {pred_text.strip()}")
            if language_code:
                console.print(f"[dim]Language: {language_code}[/dim]")

        yield Transcription(
            text=pred_text,
            language_code=language_code,
            turn_id=vad_audio.turn_id,
            turn_revision=vad_audio.turn_revision,
            speech_stopped_at_s=vad_audio.created_at_s,
            error=stt_error,
            active_speech_ms=vad_audio.active_speech_ms,
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

    def _transcribe_turn(self, audio_input: np.ndarray, turn_id: str | None) -> tuple[str, str]:
        """Transcribe a turn, reusing the part of it already transcribed.

        A pause inside one turn makes VAD re-send the whole turn so far, so
        decoding it from the start each time re-transcribed the same speech once
        per pause -- roughly eight passes over a one-minute turn with seven
        pauses. Instead, remember how many samples were already decoded and
        transcribe only the audio past that point.

        Reuse is safe because the cached prefix is a true prefix: VAD only ever
        appends to a turn's audio, and it cuts fragments at a detected end of
        speech, so the resume point sits inside real silence rather than
        mid-word. It is also self-healing: if a revision is dropped as stale,
        its audio is still past the cached mark and gets transcribed by the
        next revision.
        """
        cached_samples = self._turn_decoded_samples
        # Empty cached text is not a reason to redo the work: a fragment that
        # held no words was still transcribed, and re-transcribing silence
        # cannot produce any.
        reuse = turn_id is not None and turn_id == self._turn_decode_id and 0 < cached_samples < len(audio_input)
        if reuse:
            tail_text = self._decode_audio(audio_input[cached_samples:])
            pred_text = " ".join(part for part in (self._turn_decoded_text, tail_text) if part)
            logger.info(
                "Parakeet reused %.1fs of turn %s and decoded %.1fs of new audio",
                cached_samples / self.sample_rate,
                turn_id,
                (len(audio_input) - cached_samples) / self.sample_rate,
            )
        else:
            pred_text = self._decode_audio(audio_input)

        if turn_id is None:
            self._reset_turn_decode_cache()
        else:
            self._turn_decode_id = turn_id
            self._turn_decoded_samples = len(audio_input)
            self._turn_decoded_text = pred_text

        return pred_text, self._resolve_language(pred_text)

    def _reset_turn_decode_cache(self) -> None:
        self._turn_decode_id: str | None = None
        self._turn_decoded_samples = 0
        self._turn_decoded_text = ""

    def _decode_audio(self, audio_input: np.ndarray) -> str:
        """Decode one span of audio, in bounded windows."""
        parts: list[str] = []
        for window in iter_decode_windows(audio_input, self.sample_rate):
            window_text, _ = self._decode_mlx_window(window)
            if window_text:
                parts.append(window_text)
        return " ".join(parts)

    def _resolve_language(self, pred_text: str) -> str:
        if self.start_language and self.start_language != "auto":
            return self.start_language
        if pred_text:
            detected_lang = self._detect_language_from_text(pred_text)
            if detected_lang:
                return detected_lang
        return self.last_language

    def _decode_mlx_window(self, audio_input: np.ndarray) -> tuple[str, str]:
        import mlx.core as mx

        audio_mx = mx.array(audio_input, dtype=mx.float32)
        result = self.model.decode_chunk(audio_mx, verbose=False)
        if hasattr(result, "text"):
            pred_text = result.text.strip()
        else:
            pred_text = str(result).strip()
        # Return this decode's activation buffers to the OS. MLX keeps freed
        # buffers in a cache for reuse, but each revision of a turn decodes a
        # longer clip than the last, so the sizes never match and the cache only
        # grows: measured 4.0 GB -> 11.4 GB over one minute of pausing speech.
        # The weights (mx.get_active_memory()) never grow, so this is not a leak
        # -- it is cache that nothing was reclaiming. TTS already does this.
        mx.clear_cache()
        return pred_text, self.last_language

    def cleanup(self) -> None:
        """Clean up model resources."""
        logger.info(f"Cleaning up {self.__class__.__name__}")
        if hasattr(self, "model"):
            del self.model

    def on_session_end(self) -> None:
        super().on_session_end()
        self._reset_turn_decode_cache()
        self.last_language = self.start_language if self.start_language else "en"
        logger.debug("Parakeet TDT session state reset")
