"""Conservative transcript-quality filtering before response generation.

Audio layers (close-talk gate, Silero) decide whether something sounded like
speech. This module decides whether the transcript is talking:

- Protected commands (stop, yes, wait) always pass, even at one word.
- Filler, tagged non-speech, and Parakeet number/letter bursts never pass.
- A command or greeting may be one short word ("stop", "hello").
- Anything else needs ~480ms of *active* Silero speech. A 416ms blip can
  still transcribe as "can you hear" because STT sees padded audio; word
  count alone is not enough. Duration uses the VAD active-speech clock,
  not padded STT length.

The original policy had no minimum length, so one-word ASR junk counted as
meaningful. Commands and greetings stay on an explicit allow-list so a quick
"hello" or "stop" still starts a turn.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Literal

TurnQualityReason = Literal[
    "disabled",
    "no_text",
    "protected_short_command",
    "filler_only",
    "non_speech",
    "asr_noise",
    "too_short",
    "meaningful",
]


@dataclass(frozen=True)
class TurnQualityDecision:
    """Whether a finalized transcript should trigger a model response."""

    should_respond: bool
    reason: TurnQualityReason
    normalized_text: str

    def hide_from_client(self) -> bool:
        """True when the client should not paint this as a user bubble.

        LLM ignore and UI hide are separate: a paused sentence can be too
        short on the last fragment while still being real talking. Only
        filler, ASR number/letter bursts, and one-word blips stay invisible.
        """
        if self.should_respond:
            return False
        if self.reason in {"filler_only", "asr_noise", "non_speech", "no_text"}:
            return True
        if self.reason == "too_short":
            return len(self.normalized_text.split()) <= 1
        return False


_WORD_RE = re.compile(r"[^\W_]+(?:['’][^\W_]+)*", re.UNICODE)

# Exact bypasses document the latency/safety contract for important short
# speech.  Most would pass anyway; keeping them explicit protects them from
# future policy expansion.
_PROTECTED_SHORT_COMMANDS = frozenset(
    {
        "again",
        "cancel",
        "continue",
        "go",
        "help",
        "no",
        "nope",
        "ok",
        "okay",
        "pause",
        "repeat",
        "stop",
        "wait",
        "yes",
    }
)

# One-word talking that is not a barge-in command. Without this, "hello"
# would wait for a second word or the active-speech bar.
_ALLOWED_ONE_WORD = _PROTECTED_SHORT_COMMANDS | {
    "hello",
    "hey",
    "hi",
    "how",
    "sorry",
    "thanks",
    "what",
    "when",
    "where",
    "who",
    "why",
}

# A 416ms noise burst in the live log became USER: four, and the shortest
# real greeting we accept measured 448ms. The bar sits just above the blip so
# it still rejects it, and below everything VADHandler will finalize.
#
# Keep this at or under VADHandler's idle floor (_IDLE_TURN_MIN_SPEECH_MS).
# A bar above that floor is a dead band: VAD starts and finalizes the turn,
# the client paints the user's words, and no response is ever generated.
_MIN_ACTIVE_SPEECH_MS = 480.0

# Common ASR renderings of non-lexical hesitation.  These patterns are only
# applied when *every* token in the transcript is filler, so "um, stop" passes.
# yeah/yep/ya are backchannels, not commands. A lone "yeah" from TV in another
# room became a USER turn in the live log; "yes" stays protected above.
_FILLER_TOKEN_RE = re.compile(
    r"(?:u+h+m*|u+m+|e+r+m*|h+m+|m{2,}|a+h+|o+h+|huh|mm(?:-?hmm)?|uh-?huh|yeah|yep|ya|yea|yup)",
    re.IGNORECASE,
)

# Parakeet on brief noise often emits a lone number or "six a".
_MAX_ASR_NOISE_WORDS = 2
_NUMBER_WORDS = frozenset(
    {
        "zero",
        "oh",
        "one",
        "two",
        "three",
        "four",
        "five",
        "six",
        "seven",
        "eight",
        "nine",
        "ten",
        "eleven",
        "twelve",
        "thirteen",
        "fourteen",
        "fifteen",
        "sixteen",
        "seventeen",
        "eighteen",
        "nineteen",
        "twenty",
    }
)
_NOISE_CONNECTORS = frozenset({"and", "n", "a", "the"})
_DIGIT_RE = re.compile(r"^\d+$")
_LETTER_RE = re.compile(r"^[a-hj-z]$")  # not "i": a held "I" can be real speech

# Some STT systems emit bracketed action/non-speech labels. Untagged one-word
# "music" / "noise" is handled by the one-word rule, not this list.
_TAGGED_NON_SPEECH_PHRASES = frozenset(
    {
        "applause",
        "background noise",
        "blank audio",
        "breath",
        "breathing",
        "cough",
        "coughing",
        "inaudible",
        "laughing",
        "laughter",
        "music",
        "noise",
        "sigh",
        "sighing",
        "silence",
        "unintelligible",
        "yawn",
        "yawning",
        "yawns",
    }
)
_UNAMBIGUOUS_NON_SPEECH_PHRASES = frozenset(
    {
        "background noise",
        "blank audio",
        "inaudible",
        "unintelligible",
        "yawn",
        "yawning",
        "yawns",
    }
)


def _normalize_words(transcript: str) -> tuple[str, list[str]]:
    normalized = unicodedata.normalize("NFKC", transcript).casefold()
    words = _WORD_RE.findall(normalized)
    return " ".join(words), words


def _is_tagged_non_speech(transcript: str, normalized: str) -> bool:
    text = unicodedata.normalize("NFKC", transcript).strip()
    wrappers = (("[", "]"), ("(", ")"), ("<", ">"), ("*", "*"))
    return normalized in _TAGGED_NON_SPEECH_PHRASES and any(
        text.startswith(start) and text.rstrip(".!?").endswith(end) for start, end in wrappers
    )


def _is_asr_noise_token(word: str) -> bool:
    return (
        word in _NUMBER_WORDS
        or word in _NOISE_CONNECTORS
        or _DIGIT_RE.fullmatch(word) is not None
        or _LETTER_RE.fullmatch(word) is not None
    )


def _is_asr_noise_burst(words: list[str]) -> bool:
    """True when the whole transcript is Parakeet's number/letter noise pattern.

    Bounded so it stops eating real speech. A long *number-only* string is
    someone talking ("nine one one", "twenty twenty five"); dropping those
    also hid them from the client, so the turn vanished with no explanation.
    Parakeet's noise instead glues numbers to connectors ("six and a"), so a
    connector is what licenses the pattern beyond a two-word burst.
    """
    if len(words) > _MAX_ASR_NOISE_WORDS and not any(word in _NOISE_CONNECTORS for word in words):
        return False
    if not words or not all(_is_asr_noise_token(word) or _FILLER_TOKEN_RE.fullmatch(word) for word in words):
        return False
    return any(
        word in _NUMBER_WORDS or _DIGIT_RE.fullmatch(word) is not None or _LETTER_RE.fullmatch(word) is not None
        for word in words
    )


class TurnQualityPolicy:
    """Local text gate for finalized STT: commands, greetings, or solid talking."""

    def __init__(self, *, enabled: bool = True) -> None:
        self.enabled = enabled

    def evaluate(self, transcript: str, *, active_speech_ms: float | None = None) -> TurnQualityDecision:
        normalized, words = _normalize_words(transcript)

        if not self.enabled:
            return TurnQualityDecision(True, "disabled", normalized)
        if not words:
            return TurnQualityDecision(False, "no_text", normalized)
        if normalized in _PROTECTED_SHORT_COMMANDS:
            return TurnQualityDecision(True, "protected_short_command", normalized)
        if normalized in _UNAMBIGUOUS_NON_SPEECH_PHRASES or _is_tagged_non_speech(transcript, normalized):
            return TurnQualityDecision(False, "non_speech", normalized)
        if all(_FILLER_TOKEN_RE.fullmatch(word) for word in words):
            return TurnQualityDecision(False, "filler_only", normalized)
        if _is_asr_noise_burst(words):
            return TurnQualityDecision(False, "asr_noise", normalized)
        if any(word in _PROTECTED_SHORT_COMMANDS for word in words):
            return TurnQualityDecision(True, "protected_short_command", normalized)
        if normalized in _ALLOWED_ONE_WORD:
            return TurnQualityDecision(True, "meaningful", normalized)
        held = active_speech_ms is None or active_speech_ms >= _MIN_ACTIVE_SPEECH_MS
        if len(words) >= 2 and held:
            return TurnQualityDecision(True, "meaningful", normalized)
        if len(words) == 1 and active_speech_ms is not None and active_speech_ms >= _MIN_ACTIVE_SPEECH_MS:
            return TurnQualityDecision(True, "meaningful", normalized)
        return TurnQualityDecision(False, "too_short", normalized)
