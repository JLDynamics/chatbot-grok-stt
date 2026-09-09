"""Split reply text into runs that one Kokoro pipeline can pronounce.

Kokoro keeps one grapheme-to-phoneme front end per language. The English one
hands anything outside its lexicon to espeak-ng, and espeak-ng reads a Chinese
character as the words "Chinese letter", so a name like 华为 in an otherwise
English sentence came out as "Chinese letter Chinese letter". The fix is to
never show the English front end a Han character: text is cut into runs by
script, each run goes to the pipeline for that script, and the audio is
concatenated. Neutral characters (spaces, digits, ASCII punctuation) stay
with the run they follow so a run keeps its own phrasing.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

# Kokoro lang codes for scripts the model has voices for.
CHINESE = "z"
JAPANESE = "j"
# Kokoro-82M has no Korean voice; Hangul is dropped rather than spelled out.
UNSUPPORTED = ""

_HAN_RANGES = (
    (0x2E80, 0x2FDF),  # CJK radicals
    (0x3000, 0x303F),  # CJK symbols and punctuation (、。「」《》)
    (0x3400, 0x4DBF),  # CJK unified ideographs extension A
    (0x4E00, 0x9FFF),  # CJK unified ideographs
    (0xF900, 0xFAFF),  # CJK compatibility ideographs
    (0xFF00, 0xFF65),  # fullwidth ASCII variants (，！？：；（）)
    (0xFFE0, 0xFFEF),  # fullwidth symbols
    (0x20000, 0x2A6DF),  # CJK unified ideographs extension B
    (0x2A700, 0x2EBEF),  # extensions C-F
    (0x30000, 0x3134F),  # extension G
)
_KANA_RANGES = (
    (0x3040, 0x309F),  # hiragana
    (0x30A0, 0x30FF),  # katakana
    (0x31F0, 0x31FF),  # katakana phonetic extensions
    (0xFF66, 0xFF9F),  # halfwidth katakana
)
_HANGUL_RANGES = (
    (0x1100, 0x11FF),  # jamo
    (0x3130, 0x318F),  # compatibility jamo
    (0xA960, 0xA97F),  # jamo extended A
    (0xAC00, 0xD7AF),  # syllables
    (0xD7B0, 0xD7FF),  # jamo extended B
)

# Character classes; BASE is "whatever pipeline the turn already uses".
_BASE, _HAN, _KANA, _HANGUL, _NEUTRAL = "base", "han", "kana", "hangul", "neutral"


def _in_ranges(code: int, ranges: tuple[tuple[int, int], ...]) -> bool:
    return any(low <= code <= high for low, high in ranges)


def _classify(char: str) -> str:
    code = ord(char)
    if _in_ranges(code, _KANA_RANGES):
        return _KANA
    if _in_ranges(code, _HANGUL_RANGES):
        return _HANGUL
    if _in_ranges(code, _HAN_RANGES):
        return _HAN
    if char.isspace() or char.isdigit():
        return _NEUTRAL
    category = unicodedata.category(char)
    # Punctuation, symbols, marks and separators carry no script of their own.
    if category[0] in "PSZM":
        return _NEUTRAL
    return _BASE


@dataclass(frozen=True)
class Segment:
    """One run of text and the Kokoro lang code that should synthesize it."""

    lang_code: str
    text: str


def _runs(text: str) -> list[tuple[str, str]]:
    """Consecutive (class, text) runs; neutral characters join the current run."""
    runs: list[tuple[str, str]] = []
    current_class: str | None = None
    current: list[str] = []
    pending_neutral: list[str] = []
    for char in text:
        kind = _classify(char)
        if kind == _NEUTRAL:
            if current_class is None:
                pending_neutral.append(char)
            else:
                current.append(char)
            continue
        if kind != current_class:
            if current_class is not None:
                runs.append((current_class, "".join(current)))
            current_class = kind
            current = pending_neutral + [char]
            pending_neutral = []
        else:
            current.append(char)
    if current_class is not None:
        runs.append((current_class, "".join(current)))
    elif pending_neutral:
        runs.append((_BASE, "".join(pending_neutral)))
    return runs


def _resolve_han(runs: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Han directly next to kana is Japanese kanji, not Mandarin.

    Japanese writing alternates kanji and kana within a phrase, so a Han run
    with a kana run on either side is read by the Japanese pipeline. A Han run
    on its own, or separated from kana by Latin text, is Mandarin.
    """
    resolved: list[tuple[str, str]] = []
    for index, (kind, run) in enumerate(runs):
        if kind == _HAN:
            neighbours = {runs[i][0] for i in (index - 1, index + 1) if 0 <= i < len(runs)}
            if _KANA in neighbours:
                kind = _KANA
        resolved.append((kind, run))
    return resolved


def segment_by_script(text: str, base_lang_code: str, letters_lang_code: str | None = None) -> list[Segment]:
    """Cut *text* into pipeline-sized runs.

    Alphabetic runs (Latin, Cyrillic, ...) keep *base_lang_code*, or take
    *letters_lang_code* when given: a Mandarin turn that mentions "OpenAI"
    needs an English pipeline for that word, since the Mandarin front end
    would pass the raw letters through as phonemes. Han runs get the Mandarin
    pipeline (or Japanese when they sit beside kana); Hangul gets
    ``UNSUPPORTED``. Adjacent runs that resolve to the same pipeline are
    merged so a sentence is not needlessly split.
    """
    lang_for_class = {
        _BASE: letters_lang_code or base_lang_code,
        _HAN: CHINESE,
        _KANA: JAPANESE,
        _HANGUL: UNSUPPORTED,
    }
    segments: list[Segment] = []
    for kind, run in _resolve_han(_runs(text)):
        lang_code = lang_for_class[kind]
        if segments and segments[-1].lang_code == lang_code:
            segments[-1] = Segment(lang_code, segments[-1].text + run)
        else:
            segments.append(Segment(lang_code, run))
    return segments


def contains_han(text: str) -> bool:
    return any(_classify(char) == _HAN for char in text)
