"""Kokoro-82M conversational TTS handler for Apple Silicon (via mlx-audio).

Matches the official speech-to-speech Kokoro setup: ``mlx-community/Kokoro-82M-bf16``
on Apple Silicon, ``bm_fable`` (British male) by default, automatic language ->
Kokoro-lang-code -> voice mapping from the STT language.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from queue import Queue
from threading import Event
from typing import Any

from chatbot.pipeline.cancel_scope import CancelScope
from chatbot.pipeline.messages import TTSInput
from chatbot.pipeline.queue_types import TextEventItem
from chatbot.pipeline.speculative_turns import SpeculativeTurnTracker
from chatbot.TTS.base_tts_handler import BaseTTSHandler

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "mlx-community/Kokoro-82M-bf16"
# Kokoro voice naming: <lang><gender>_<name>. Default = British male.
DEFAULT_VOICE = "bm_fable"
DEFAULT_LANG_CODE = "b"
DEFAULT_SPEED = 1.0
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


class KokoroTTSHandler(BaseTTSHandler):
    """Synthesize speech with Kokoro-82M (via mlx-audio) on Apple Silicon."""

    backend_name = "Kokoro"

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
        self.voice = voice
        self.lang_code = lang_code
        self.speed = float(speed)
        self._common_setup(
            should_listen, gen_kwargs, cancel_scope, speculative_turns, text_output_queue, model_name, blocksize
        )
        self.model = self._load_mlx_model(
            "Kokoro TTS on Apple Silicon requires mlx-audio plus its TTS deps "
            "(misaki, espeakng-loader, num2words, spacy, phonemizer-fork). Run uv sync."
        )
        self.warmup()

    def _generate(self, text: str, gen_kwargs: dict[str, Any]) -> Iterable[Any]:
        gen_kwargs = dict(gen_kwargs)
        speed = float(gen_kwargs.pop("speed", self.speed))
        lang_code = gen_kwargs.pop("lang_code", self.lang_code)
        return self.model.generate(
            text=text,
            voice=self.voice,
            speed=speed,
            lang_code=lang_code,
            **gen_kwargs,
        )

    def _apply_voice(self, tts_input: TTSInput) -> None:
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
