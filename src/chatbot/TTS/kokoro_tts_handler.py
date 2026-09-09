"""Kokoro-82M conversational TTS handler for Apple Silicon (via mlx-audio).

Matches the official speech-to-speech Kokoro setup: ``mlx-community/Kokoro-82M-bf16``
on Apple Silicon, ``bm_fable`` (British male) by default, automatic language ->
Kokoro-lang-code -> voice mapping from the STT language.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from queue import Queue
from threading import Event
from typing import Any

import numpy as np

from chatbot.pipeline.cancel_scope import CancelScope
from chatbot.pipeline.messages import TTSInput
from chatbot.pipeline.queue_types import TextEventItem
from chatbot.pipeline.speculative_turns import SpeculativeTurnTracker
from chatbot.TTS.base_tts_handler import BaseTTSHandler
from chatbot.TTS.script_segments import UNSUPPORTED, Segment, segment_by_script
from chatbot.TTS.tts_common import trim_edge_silence

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
# (male, female) voices used when a run of another script appears inside a
# reply, so the inserted voice at least matches the speaker's gender. Kokoro
# voices are named <lang><gender>_<name>; French has no male voice.
KOKORO_LANG_GENDERED_VOICES: dict[str, tuple[str, str]] = {
    "a": ("am_michael", "af_heart"),
    "b": ("bm_fable", "bf_emma"),
    "e": ("em_alex", "ef_dora"),
    "f": ("ff_siwis", "ff_siwis"),
    "h": ("hm_omega", "hf_alpha"),
    "i": ("im_nicola", "if_sara"),
    "j": ("jm_kumo", "jf_alpha"),
    "p": ("pm_alex", "pf_dora"),
    "z": ("zm_yunyang", "zf_xiaoxiao"),
}
# Pipelines whose own script is alphabetic. Any other base pipeline (Mandarin,
# Japanese, Hindi) hands Latin runs such as "OpenAI" to an English one.
ALPHABETIC_PIPELINES = frozenset({"a", "b", "e", "f", "i", "p"})
ENGLISH_PIPELINES = frozenset({"a", "b"})
# Mixed-script warmup: loads the Mandarin front end (jieba's dictionary build
# takes a moment) and its voice before the first conversation needs them.
MIXED_WARMUP_TEXT = "Huawei, or 华为, is a company."
# Silence kept on each side of a join between two runs. Kokoro leaves
# 0.2-0.7 s at every edge; together these give a short inter-word gap instead.
RUN_BOUNDARY_LEAD_S = 0.06
RUN_BOUNDARY_TRAIL_S = 0.12


@dataclass(frozen=True)
class _AudioChunk:
    """A trimmed run, shaped like the chunks mlx-audio yields (``.audio``)."""

    audio: np.ndarray


def voice_for_inserted_script(lang_code: str, base_voice: str) -> str:
    """The voice for a *lang_code* run inside a reply spoken by *base_voice*."""
    male, female = KOKORO_LANG_GENDERED_VOICES.get(lang_code, (None, None))
    if male is None or female is None:
        return KOKORO_LANG_DEFAULT_VOICES.get(lang_code, base_voice)
    gender = base_voice[1:2].lower() if len(base_voice) > 1 else "m"
    return female if gender == "f" else male


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
        # The English pipeline for Latin runs inside a Mandarin/Japanese turn.
        self.english_lang_code = lang_code if lang_code in ENGLISH_PIPELINES else DEFAULT_LANG_CODE
        # Pipelines whose front end failed to import; their runs are dropped
        # instead of failing every turn that mentions them.
        self._unavailable_pipelines: set[str] = set()
        self._common_setup(
            should_listen, gen_kwargs, cancel_scope, speculative_turns, text_output_queue, model_name, blocksize
        )
        self.model = self._load_mlx_model(
            "Kokoro TTS on Apple Silicon requires mlx-audio plus its TTS deps "
            "(misaki, espeakng-loader, num2words, spacy, phonemizer-fork). Run uv sync."
        )
        self.warmup()

    def warmup(self) -> None:
        super().warmup()
        try:
            for _ in self._process(MIXED_WARMUP_TEXT):
                pass
        except Exception as exc:  # startup remains usable when a warmup-only call fails
            logger.warning("%s mixed-script warmup failed: %s", self.backend_name, exc)

    def _generate(self, text: str, gen_kwargs: dict[str, Any]) -> Iterable[Any]:
        gen_kwargs = dict(gen_kwargs)
        speed = float(gen_kwargs.pop("speed", self.speed))
        lang_code = gen_kwargs.pop("lang_code", self.lang_code)
        letters = None if lang_code in ALPHABETIC_PIPELINES else self.english_lang_code
        segments = []
        for segment in segment_by_script(text, base_lang_code=lang_code, letters_lang_code=letters):
            if segment.lang_code == UNSUPPORTED or segment.lang_code in self._unavailable_pipelines:
                if segment.text.strip():
                    logger.warning("Kokoro has no pipeline for %r; skipping it", segment.text.strip())
                continue
            segments.append(segment)
        if not segments:
            return
        if len(segments) == 1:
            # The usual single-script reply streams chunk by chunk as before.
            yield from self._generate_run(segments[0], lang_code, speed, gen_kwargs)
            return
        # A mixed reply is spliced from short runs; each is buffered so the
        # pause the model puts around every run can be cut down to a natural
        # gap where two runs meet.
        logger.info("Kokoro mixed-script reply: %s", " ".join(f"[{s.lang_code}]{s.text.strip()!r}" for s in segments))
        for index, segment in enumerate(segments):
            chunks = [
                np.asarray(item.audio, dtype=np.float32)
                for item in self._generate_run(segment, lang_code, speed, gen_kwargs)
            ]
            if not chunks:
                continue
            audio = trim_edge_silence(
                np.concatenate(chunks),
                self.model.sample_rate,
                keep_lead_s=None if index == 0 else RUN_BOUNDARY_LEAD_S,
                keep_trail_s=None if index == len(segments) - 1 else RUN_BOUNDARY_TRAIL_S,
            )
            yield _AudioChunk(audio)

    def _generate_run(
        self, segment: Segment, lang_code: str, speed: float, gen_kwargs: dict[str, Any]
    ) -> Iterator[Any]:
        """Model chunks for one run, or nothing when its front end cannot load."""
        voice = (
            self.voice if segment.lang_code == lang_code else voice_for_inserted_script(segment.lang_code, self.voice)
        )
        try:
            yield from self.model.generate(
                text=segment.text,
                voice=voice,
                speed=speed,
                lang_code=segment.lang_code,
                **gen_kwargs,
            )
        except ImportError as exc:
            # e.g. misaki[ja] is not installed. Say the rest of the reply.
            self._unavailable_pipelines.add(segment.lang_code)
            logger.warning(
                "Kokoro cannot synthesize %r (lang %s): %s. Runs in that script will be skipped.",
                segment.text.strip(),
                segment.lang_code,
                exc,
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
