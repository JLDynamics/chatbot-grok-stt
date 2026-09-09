from datetime import datetime, timezone

from chatbot.LLM.text_prompt import build_text_system_prompt

NOW = datetime(2026, 9, 8, 21, 35, tzinfo=timezone.utc)


def test_text_prompt_keeps_persona_in_session_prompt():
    prompt = build_text_system_prompt("Be helpful.", now=NOW)

    assert "Be helpful." in prompt
    assert "You are a helpful assistant in a text conversation." in prompt
    assert "Current date and time: Tuesday, September 8, 2026, 9:35 PM (UTC)." in prompt


def test_text_prompt_allows_markdown_and_drops_voice_rules():
    prompt = build_text_system_prompt("Be helpful.", memory="- Likes terse answers.", now=NOW)

    assert "Use markdown when it helps" in prompt
    assert "Use web_search on your own initiative" in prompt
    assert "- Likes terse answers." in prompt
    # No spoken-channel rules leak into the text prompt.
    assert "Speech is the default" not in prompt
    assert "Treat speech transcripts as imperfect" not in prompt
    assert "Say one short natural line before a slow tool" not in prompt
