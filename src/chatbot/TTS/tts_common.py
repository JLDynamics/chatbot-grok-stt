"""Shared TTS turn helpers."""

from __future__ import annotations

from collections import deque
from typing import Any

from chatbot.pipeline.messages import TTSInput


def drop_queued_tts_inputs(queue_in: Any, turn_id: str | None, turn_revision: int | None) -> int:
    """Remove queued ``TTSInput``s for the failed turn so they cannot restart it."""
    if not hasattr(queue_in, "mutex") or not hasattr(queue_in, "queue"):
        return 0
    dropped = 0
    with queue_in.mutex:
        kept: deque[Any] = deque()
        for item in queue_in.queue:
            if isinstance(item, TTSInput) and (item.turn_id, item.turn_revision) == (turn_id, turn_revision):
                dropped += 1
                continue
            kept.append(item)
        queue_in.queue = kept
    return dropped
