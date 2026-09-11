from datetime import datetime, timedelta, timezone

from chatbot.LLM.voice_prompt import (
    VOICE_SYSTEM_PROMPT_LEAD,
    VOICE_SYSTEM_PROMPT_TAIL,
    build_voice_system_prompt,
    format_now,
)

PERSONA = "You are an AI conversation partner: perceptive, relaxed, warm, and quietly playful."
NOW = datetime(2026, 9, 8, 21, 35, tzinfo=timezone(timedelta(hours=-6), "MDT"))


def test_voice_prompt_preserves_persona_and_spoken_constraints():
    prompt = build_voice_system_prompt("Be concise and dryly funny.", now=NOW)

    assert "Be concise and dryly funny." in prompt
    assert "Tools run inside the spoken reply" in prompt
    assert "no Markdown, headings, bullets, emoji" in prompt
    assert "Treat speech transcripts as imperfect." in prompt


def test_voice_prompt_tells_the_model_the_date_and_when_to_search():
    prompt = build_voice_system_prompt("Be concise.", now=NOW)

    assert "Current date and time: Tuesday, September 8, 2026, 9:35 PM (MDT)." in prompt
    assert "You drive research yourself with bash (curl)" in prompt
    assert "You drive research yourself with bash (curl) in this same reply" in prompt
    assert "On every question, decide for yourself whether your knowledge is still current" in prompt
    assert "Do not wait for the user to tell you that you were wrong" in prompt
    assert "Stable facts" in prompt
    assert "curl Wikipedia or an official page" in prompt
    assert "There is no web_search or read_page tool." in prompt
    assert "when:1d" in prompt
    assert "keep only the last 24 hours" in prompt
    assert "a new article about an old event is not happening today" in prompt
    assert "Several tool calls in one round are fine." in prompt
    assert "Keep research quick" in prompt
    # The old rules that stopped proactive research are gone.
    assert "Use at most one tool" not in prompt
    assert "If unsure whether a tool is needed, just speak" not in prompt
    assert "Speech is the default" not in prompt


def test_voice_prompt_is_compact():
    prompt = build_voice_system_prompt(PERSONA, now=NOW)
    assert len(prompt) < 5200, f"voice prompt grew to {len(prompt)} chars; every turn pays for it"


def test_memory_block_is_included_only_when_present():
    bare = build_voice_system_prompt(PERSONA, now=NOW)
    assert "## About the user" not in bare

    with_memory = build_voice_system_prompt(PERSONA, memory="- Jack works at Costco.\n", now=NOW)
    assert "## About the user" in with_memory
    assert "- Jack works at Costco." in with_memory
    # Memory is context, so it sits before the rules that must win.
    assert with_memory.index("Jack works at Costco") < with_memory.index("## Voice Rules")


def test_personality_is_opening_and_rules_follow():
    assert "perceptive, relaxed, warm, and quietly playful" in VOICE_SYSTEM_PROMPT_LEAD
    assert "Match the depth of your reply to the user's intent" in VOICE_SYSTEM_PROMPT_LEAD
    assert VOICE_SYSTEM_PROMPT_TAIL.strip().startswith("## Knowledge and research")
    assert VOICE_SYSTEM_PROMPT_TAIL.index("## Knowledge and research") < VOICE_SYSTEM_PROMPT_TAIL.index(
        "## Voice Rules"
    )


def test_identity_survives_a_long_session_tool_block():
    tool_blob = "TOOL ROUTING\n" + ("use read_page. " * 400)
    prompt = build_voice_system_prompt(PERSONA + "\n" + tool_blob, now=NOW)

    personality = prompt.find("perceptive, relaxed, warm, and quietly playful")
    last_tool = prompt.rfind("use read_page.")
    identity = prompt.rfind("Do not take on a branded product name")
    tools_inside = prompt.find("Tools run inside the spoken reply")

    assert personality != -1
    assert personality < last_tool < identity
    assert tools_inside > last_tool


def test_format_now_reads_like_speech():
    assert format_now(datetime(2026, 1, 1, 0, 5)) == "Thursday, January 1, 2026, 12:05 AM"
    assert format_now(datetime(2026, 12, 25, 13, 0, tzinfo=timezone.utc)) == "Friday, December 25, 2026, 1:00 PM (UTC)"
