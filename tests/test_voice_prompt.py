from chatbot.LLM.voice_prompt import VOICE_SYSTEM_PROMPT, VOICE_SYSTEM_PROMPT_LEAD, VOICE_SYSTEM_PROMPT_TAIL, build_voice_system_prompt

PERSONA = (
    "You are an AI conversation partner: perceptive, relaxed, warm, "
    "and quietly playful."
)


def test_voice_prompt_preserves_persona_and_spoken_constraints():
    prompt = build_voice_system_prompt("Be concise and dryly funny.")

    assert "Be concise and dryly funny." in prompt
    assert "Speech is the default." in prompt
    assert "ordinary speech without Markdown, headings, bullets" in prompt
    assert "Treat speech transcripts as imperfect." in prompt


def test_voice_prompt_keeps_tools_bounded_and_natural():
    prompt = build_voice_system_prompt("Be concise.")
    tail_from_tools = VOICE_SYSTEM_PROMPT_TAIL.split("## Voice Rules", 1)[-1]

    assert len(tail_from_tools.split()) < 230
    assert "Use at most one tool" in prompt
    assert "Before a tool call, use a brief natural utterance" in prompt
    assert "immediately chaining from a metadata-only routing/preflight tool" in prompt
    assert "If unsure whether a tool is needed, just speak." in prompt
    assert "one-line question gets a one-line answer" not in VOICE_SYSTEM_PROMPT


def test_personality_is_opening_and_tool_rules_follow():
    assert "perceptive, relaxed, warm, and quietly playful" in VOICE_SYSTEM_PROMPT_LEAD
    assert "Match the depth of your reply to the user's intent" in VOICE_SYSTEM_PROMPT_LEAD
    assert VOICE_SYSTEM_PROMPT_TAIL.strip().startswith("## Voice Rules")
    assert VOICE_SYSTEM_PROMPT_TAIL.index("Speech is the default.") < VOICE_SYSTEM_PROMPT_TAIL.index(
        "You are the conversation partner described above."
    )


def test_identity_survives_a_long_session_tool_block():
    tool_blob = "TOOL ROUTING\n" + ("use read_page. " * 400)
    prompt = build_voice_system_prompt(PERSONA + "\n" + tool_blob)

    personality = prompt.find("perceptive, relaxed, warm, and quietly playful")
    last_tool = prompt.rfind("use read_page.")
    identity = prompt.rfind("Do not take on a branded product name")
    speech_default = prompt.find("Speech is the default.")

    assert personality != -1
    assert personality < last_tool < identity
    assert speech_default > last_tool
