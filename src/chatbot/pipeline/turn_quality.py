"""Conservative transcript-quality filtering before response generation.

The browser noise gate and Silero VAD decide whether audio sounds like speech,
while Smart Turn decides whether that speech sounds complete.  This module is
the final, text-level layer: it prevents finalized filler and explicit
non-speech labels from becoming LLM turns.

The policy deliberately has no minimum length.  Any text not confidently
recognized as filler/non-speech is allowed, so short commands remain usable.
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
    "meaningful",
]


@dataclass(frozen=True)
class TurnQualityDecision:
    """Whether a finalized transcript should trigger a model response."""

    should_respond: bool
    reason: TurnQualityReason
    normalized_text: str


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
        "yeah",
        "yep",
        "yes",
    }
)

# Common ASR renderings of non-lexical hesitation.  These patterns are only
# applied when *every* token in the transcript is filler, so "um, stop" passes.
_FILLER_TOKEN_RE = re.compile(r"(?:u+h+m*|u+m+|e+r+m*|h+m+|m{2,})", re.IGNORECASE)

# Some STT systems emit bracketed action/non-speech labels.  Plain single words
# such as "music" remain allowed because they can also be real short commands.
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


class TurnQualityPolicy:
    """Fast, local allow-by-default policy for finalized STT text."""

    def __init__(self, *, enabled: bool = True) -> None:
        self.enabled = enabled

    def evaluate(self, transcript: str) -> TurnQualityDecision:
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
        return TurnQualityDecision(True, "meaningful", normalized)
