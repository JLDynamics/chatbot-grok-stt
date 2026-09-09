"""Kokoro routes each script in a reply to the pipeline that can pronounce it."""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from chatbot.TTS.kokoro_tts_handler import (
    RUN_BOUNDARY_LEAD_S,
    RUN_BOUNDARY_TRAIL_S,
    KokoroTTSHandler,
    voice_for_inserted_script,
)
from chatbot.TTS.tts_common import trim_edge_silence

SR = 24000
# Every generated run: 0.3 s silence, 0.5 s speech, 0.5 s silence, like Kokoro.
LEAD, SPEECH, TRAIL = int(0.3 * SR), int(0.5 * SR), int(0.5 * SR)


def run_audio() -> np.ndarray:
    return np.concatenate([np.zeros(LEAD), np.full(SPEECH, 0.2), np.zeros(TRAIL)]).astype(np.float32)


class FakeKokoro:
    """Records generate() calls; each yields one run of audio."""

    sample_rate = SR

    def __init__(self, missing: set[str] | None = None) -> None:
        self.calls: list[dict] = []
        self.missing = missing or set()

    def generate(self, text, voice, speed, lang_code, **kwargs):
        if lang_code in self.missing:
            raise ImportError(f"Kokoro requires misaki[{lang_code}]")
        self.calls.append({"text": text, "voice": voice, "speed": speed, "lang_code": lang_code, **kwargs})
        yield SimpleNamespace(audio=run_audio())


def handler(voice="bm_fable", lang_code="b", model=None) -> KokoroTTSHandler:
    tts = KokoroTTSHandler.__new__(KokoroTTSHandler)
    tts.voice = voice
    tts.lang_code = lang_code
    tts.speed = 1.0
    tts.english_lang_code = lang_code if lang_code in ("a", "b") else "b"
    tts._unavailable_pipelines = set()
    tts.model = model or FakeKokoro()
    return tts


def test_english_reply_uses_one_pipeline_and_the_configured_voice():
    tts = handler()
    chunks = list(tts._generate("Hello there.", {}))
    assert len(chunks) == 1
    assert tts.model.calls == [{"text": "Hello there.", "voice": "bm_fable", "speed": 1.0, "lang_code": "b"}]
    # A single-script reply streams the model's chunks untouched.
    assert len(chunks[0].audio) == LEAD + SPEECH + TRAIL


def test_joins_between_runs_keep_only_a_short_gap():
    tts = handler()
    chunks = [np.asarray(c.audio) for c in tts._generate("Huawei, or 华为, is a company.", {})]
    assert len(chunks) == 3
    first, middle, last = chunks
    # The reply keeps its natural silence at the very start and very end...
    assert len(first) == LEAD + SPEECH + int(RUN_BOUNDARY_TRAIL_S * SR)
    assert len(middle) == int(RUN_BOUNDARY_LEAD_S * SR) + SPEECH + int(RUN_BOUNDARY_TRAIL_S * SR)
    assert len(last) == int(RUN_BOUNDARY_LEAD_S * SR) + SPEECH + TRAIL
    # ...and no speech was cut.
    assert all(np.count_nonzero(chunk) == SPEECH for chunk in chunks)


def test_trim_edge_silence_leaves_all_silent_or_empty_audio_alone():
    silent = np.zeros(1000, dtype=np.float32)
    assert trim_edge_silence(silent, SR, keep_lead_s=0.0, keep_trail_s=0.0) is silent
    empty = np.zeros(0, dtype=np.float32)
    assert trim_edge_silence(empty, SR, keep_lead_s=0.0, keep_trail_s=0.0) is empty
    audio = run_audio()
    assert len(trim_edge_silence(audio, SR, keep_lead_s=None, keep_trail_s=None)) == len(audio)
    assert len(trim_edge_silence(audio, SR, keep_lead_s=1.0, keep_trail_s=1.0)) == len(audio)


def test_chinese_run_in_an_english_reply_goes_to_mandarin_with_a_matching_male_voice():
    tts = handler()
    list(tts._generate("Huawei, or 华为, is a company.", {"speed": 1.1}))
    assert [(c["lang_code"], c["voice"], c["text"]) for c in tts.model.calls] == [
        ("b", "bm_fable", "Huawei, or "),
        ("z", "zm_yunyang", "华为, "),
        ("b", "bm_fable", "is a company."),
    ]
    assert all(c["speed"] == 1.1 for c in tts.model.calls)


def test_female_speaker_gets_a_female_mandarin_voice():
    tts = handler(voice="af_heart", lang_code="a")
    list(tts._generate("小墨同学 is a robot.", {}))
    assert tts.model.calls[0]["voice"] == "zf_xiaoxiao"
    assert tts.model.calls[1]["voice"] == "af_heart"


def test_mandarin_turn_hands_latin_words_to_the_english_pipeline():
    tts = handler(voice="zf_xiaobei", lang_code="z")
    list(tts._generate("我用 OpenAI 的模型。", {}))
    assert [(c["lang_code"], c["voice"], c["text"]) for c in tts.model.calls] == [
        ("z", "zf_xiaobei", "我用 "),
        ("b", "bf_emma", "OpenAI "),
        ("z", "zf_xiaobei", "的模型。"),
    ]


def test_spanish_turn_keeps_its_own_pipeline_for_latin_text():
    tts = handler(voice="ef_dora", lang_code="e")
    list(tts._generate("Hola, OpenAI es una empresa.", {}))
    assert [c["lang_code"] for c in tts.model.calls] == ["e"]


def test_unsupported_script_is_dropped_and_the_rest_is_spoken(caplog):
    tts = handler()
    with caplog.at_level("WARNING"):
        chunks = list(tts._generate("Samsung, or 삼성, is Korean.", {}))
    assert len(chunks) == 2
    assert [c["text"] for c in tts.model.calls] == ["Samsung, or ", "is Korean."]
    assert "삼성" in caplog.text


def test_reply_that_is_only_unsupported_script_yields_no_audio(caplog):
    tts = handler()
    with caplog.at_level("WARNING"):
        assert list(tts._generate("삼성", {})) == []
    assert tts.model.calls == []


def test_missing_pipeline_dependency_skips_that_script_from_then_on(caplog):
    tts = handler(model=FakeKokoro(missing={"j"}))
    with caplog.at_level("WARNING"):
        chunks = list(tts._generate("Tokyo is 東京です in Japanese.", {}))
        assert len(chunks) == 2
        assert "j" in tts._unavailable_pipelines
        list(tts._generate("Again: 東京です.", {}))
    assert [c["text"] for c in tts.model.calls] == ["Tokyo is ", "in Japanese.", "Again: "]
    assert "misaki[j]" in caplog.text


def test_explicit_lang_code_kwarg_overrides_the_turn_language():
    tts = handler()
    list(tts._generate("Bonjour tout le monde.", {"lang_code": "f"}))
    assert tts.model.calls == [{"text": "Bonjour tout le monde.", "voice": "bm_fable", "speed": 1.0, "lang_code": "f"}]


@pytest.mark.parametrize(
    ("lang_code", "base_voice", "expected"),
    [
        ("z", "bm_fable", "zm_yunyang"),
        ("z", "bf_emma", "zf_xiaoxiao"),
        ("z", "af_heart", "zf_xiaoxiao"),
        ("j", "am_michael", "jm_kumo"),
        ("f", "bm_fable", "ff_siwis"),  # French has no male voice
        ("b", "zf_xiaobei", "bf_emma"),
        ("q", "bm_fable", "bm_fable"),  # unknown pipeline: keep the speaker
    ],
)
def test_voice_for_inserted_script(lang_code, base_voice, expected):
    assert voice_for_inserted_script(lang_code, base_voice) == expected
