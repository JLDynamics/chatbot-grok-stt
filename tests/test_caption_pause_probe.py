"""Caption reducer used by the pause-continue live probe."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("verify_voice", ROOT / "scripts" / "verify-voice.py")
assert SPEC is not None and SPEC.loader is not None
verify_voice = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = verify_voice
SPEC.loader.exec_module(verify_voice)


def test_merge_user_text_keeps_prefix_on_pause_fragment():
    merged = verify_voice.merge_user_text("yeah i still need to finish", "a lot of work to do")
    assert "yeah i still need to finish" in merged
    assert "a lot of work to do" in merged


def test_merge_user_text_replaces_shorter_restatement():
    merged = verify_voice.merge_user_text(
        "i still need to finish a lot of work to do",
        "i still need to finish a lot of work today",
    )
    assert merged == "i still need to finish a lot of work today"


def test_reduce_live_captions_does_not_vanish_on_empty_completed():
    events = [
        {
            "type": "conversation.item.input_audio_transcription.delta",
            "item_id": "item-1",
            "delta": "yeah i still need to finish",
        },
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "item-1",
            "transcript": "",
        },
        {
            "type": "conversation.item.input_audio_transcription.delta",
            "item_id": "item-1",
            "delta": "a lot of work to do",
        },
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "item-1",
            "transcript": "yeah i still need to finish a lot of work to do",
        },
    ]
    reduced = verify_voice.reduce_live_captions(events)
    assert reduced["vanished"] is False
    assert "yeah i still need to finish" in reduced["displayed"]
    assert "a lot of work to do" in reduced["displayed"]
    assert reduced["item_ids"] == ["item-1"]


def test_reduce_live_captions_merges_split_item_ids():
    events = [
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "item-a",
            "transcript": "yeah i still need to finish",
        },
        {
            "type": "conversation.item.input_audio_transcription.delta",
            "item_id": "item-b",
            "delta": "a lot of work to do",
        },
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "item-b",
            "transcript": "a lot of work to do",
        },
    ]
    reduced = verify_voice.reduce_live_captions(events)
    assert reduced["vanished"] is False
    assert "yeah i still need to finish" in reduced["displayed"]
    assert "a lot of work to do" in reduced["displayed"]
