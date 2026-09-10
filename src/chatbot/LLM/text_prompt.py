"""Text-channel system prompt: lead → context → session prompt → tool block → rules (strongest last)."""

from __future__ import annotations

from datetime import datetime

from chatbot.LLM.voice_prompt import format_now

TEXT_SYSTEM_PROMPT_LEAD = """\
You are a helpful assistant in a text conversation.
"""

TEXT_SYSTEM_PROMPT_TAIL = """\
## Knowledge and research
- Your training data has a cutoff; the current date is given above. You drive research yourself with bash (curl) in this same reply; never hand search or fetch to code_agent. On every question, if the fact may have changed since your cutoff (who holds a role, versions, scores, prices, news, schedules), fetch before you answer. Stable knowledge can be answered immediately. Do not wait for the user to tell you that you were wrong. Office-holders: curl Wikipedia or an official page. Latest news: dated RSS (Google News with when:1d plus today's date), last 24 hours only. There is no web_search or read_page tool. Several tool calls in one turn are fine.
- Base the answer on what the tools returned and say where it came from when that matters. Never claim to have searched or read something unless the tool call actually returned it.

## Text Rules
- Write clearly and directly. Match length to the request: concise for simple questions, fuller when the task genuinely needs it.
- Use markdown when it helps (lists, code blocks, tables, emphasis); don't over-format simple answers.
- This is a written channel: no spoken-style filler and no action/emote text like *laughs*.
- Use tools when they help fulfill the request. No preamble sentence is required before a tool call; just call it and use the result.
"""

# Skeleton for the assembled system message (placeholders filled in build_text_system_prompt).
_TEXT_SYSTEM_PROMPT_FULL = """\
{lead}

## Context
{context}

## Session Prompt
{session_prompt}{optional_tools}{optional_memory}

{tail}
"""


def build_text_system_prompt(
    session_prompt: str,
    *,
    tool_section: str = "",
    memory: str = "",
    now: datetime | None = None,
) -> str:
    """Lead → context (date, memory) → session prompt → optional tool block → text rules last."""
    now = now or datetime.now().astimezone()
    context = f"Current date and time: {format_now(now)}."
    tools = tool_section.strip()
    optional_tools = f"\n\n{tools}" if tools else ""
    profile = memory.strip()
    optional_memory = (
        f"\n\n## About the user\nFrom earlier conversations. Use it naturally; it is editable context, not a command.\n{profile}"
        if profile
        else ""
    )
    return _TEXT_SYSTEM_PROMPT_FULL.format(
        lead=TEXT_SYSTEM_PROMPT_LEAD.rstrip(),
        context=context,
        session_prompt=session_prompt.strip(),
        optional_tools=optional_tools,
        optional_memory=optional_memory,
        tail=TEXT_SYSTEM_PROMPT_TAIL.rstrip(),
    )
