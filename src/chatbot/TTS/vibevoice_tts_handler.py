"""Microsoft VibeVoice conversational TTS handler for Apple Silicon (mlx-audio)."""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from queue import Queue
from threading import Event
from typing import Any

import numpy as np

from chatbot.pipeline.cancel_scope import CancelScope
from chatbot.pipeline.messages import TTSInput
from chatbot.pipeline.queue_types import TextEventItem
from chatbot.pipeline.speculative_turns import SpeculativeTurnTracker
from chatbot.TTS.base_tts_handler import BaseTTSHandler

logger = logging.getLogger(__name__)

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


class VibeVoiceTTSHandler(BaseTTSHandler):
    """Synthesize speech with Microsoft VibeVoice (streaming) via mlx-audio."""

    backend_name = "VibeVoice"

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
        self.voice = voice
        self.max_tokens = int(max_tokens)
        self.cfg_scale = float(cfg_scale)
        self.ddpm_steps = ddpm_steps
        self._resampler: Any = None
        self._common_setup(should_listen, gen_kwargs, cancel_scope, speculative_turns, text_output_queue, model_name)
        self.model = self._load_mlx_model("VibeVoice TTS requires mlx-audio and its TTS dependencies. Run uv sync.")
        self.warmup()

    def _generate(self, text: str, gen_kwargs: dict[str, Any]) -> Iterable[Any]:
        import soxr

        gen_kwargs = dict(gen_kwargs)
        max_tokens = int(gen_kwargs.pop("max_tokens", self.max_tokens))
        cfg_scale = float(gen_kwargs.pop("cfg_scale", self.cfg_scale))
        ddpm_steps = gen_kwargs.pop("ddpm_steps", self.ddpm_steps)
        # Resample to the pipeline output rate only if it differs from the
        # model's native rate (VibeVoice is 24 kHz; the pipeline now plays
        # 24 kHz, so this is a pass-through for the default model).
        use_resample = MODEL_SR != PIPELINE_SR
        self._resampler = soxr.ResampleStream(MODEL_SR, PIPELINE_SR, 1, dtype="float32") if use_resample else None
        # VibeVoice yields GenerationResult chunks as it decodes; the model
        # stops on EOS, so max_tokens is only a safety ceiling.
        return self.model.generate(
            text=text,
            voice=self.voice,
            max_tokens=max_tokens,
            cfg_scale=cfg_scale,
            ddpm_steps=ddpm_steps,
            verbose=False,
            **gen_kwargs,
        )

    def _map_chunk(self, audio: np.ndarray) -> np.ndarray:
        if self._resampler is None:
            return audio
        return np.asarray(self._resampler.resample_chunk(np.ascontiguousarray(audio), last=False), dtype=np.float32)

    def _model_tail(self) -> np.ndarray:
        if self._resampler is None:
            return np.zeros(0, dtype=np.float32)
        return np.asarray(self._resampler.resample_chunk(np.zeros(0, dtype=np.float32), last=True), dtype=np.float32)

    def _apply_voice(self, tts_input: TTSInput) -> None:
        # Only switch to a voice the model actually ships (so load_voice never fails).
        voice: str | None = None
        response: Any = tts_input.response
        audio_in: Any = getattr(response, "audio", None) if response else None
        if audio_in is not None and audio_in.output and audio_in.output.voice:
            voice = str(audio_in.output.voice)
        if voice is None and tts_input.runtime_config is not None:
            audio = tts_input.runtime_config.session.audio
            output = audio.output if audio is not None else None
            if output is not None and output.voice:
                voice = str(output.voice)
        if voice and VOICE_LOOKS_VIBEVOICE.match(voice) and voice != self.voice:
            logger.info("VibeVoice voice set to %s", voice)
            self.voice = voice
