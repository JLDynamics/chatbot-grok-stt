from chatbot.LLM.voice_prompt import VOICE_SYSTEM_PROMPT, build_voice_system_prompt


def test_voice_prompt_preserves_persona_and_spoken_constraints():
    prompt = build_voice_system_prompt("Be concise and dryly funny.")

    assert "Be concise and dryly funny." in prompt
    assert "Speech is the default." in prompt
    assert "No markdown, bullets, headings" in prompt
    assert "Treat transcripts as noisy." in prompt


def test_voice_prompt_keeps_tools_bounded_and_natural():
    prompt = build_voice_system_prompt("Be concise.")

    assert len(VOICE_SYSTEM_PROMPT.split()) < 230
    assert "Use at most one tool" in prompt
    assert "Before a tool call, use a brief natural utterance" in prompt
    assert "immediately chaining from a metadata-only routing/preflight tool" in prompt
    assert "If unsure whether a tool is needed, just speak." in prompt
