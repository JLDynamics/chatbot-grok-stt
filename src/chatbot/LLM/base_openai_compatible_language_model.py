from __future__ import annotations

import logging
import os
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Generator, Iterator
from typing import Any, Optional

import httpx
from nltk import sent_tokenize
from openai import OpenAI
from openai.types.realtime.conversation_item import (
    RealtimeConversationItemAssistantMessage,
    RealtimeConversationItemFunctionCall,
    RealtimeConversationItemFunctionCallOutput,
)
from openai.types.realtime.realtime_conversation_item_assistant_message import (
    Content as AssistantContent,
)
from openai.types.responses import ResponseFunctionToolCall
from pydantic import BaseModel, ConfigDict, Field

from chatbot.baseHandler import BaseHandler
from chatbot.LLM.chat import Chat, ChatItemError, SupportedItem
from chatbot.LLM.chat_factories import build_active_chat, make_system_message, make_user_message
from chatbot.LLM.compaction_prompt import CompactGenerateFn, build_compactor
from chatbot.LLM.server_tools import MAX_TOOL_ROUNDS, TOOL_TIME_BUDGET_S, ServerToolExecutor
from chatbot.LLM.text_prompt import build_text_system_prompt
from chatbot.LLM.utils import remove_unspeechable, resolve_auto_language
from chatbot.LLM.voice_prompt import build_voice_system_prompt
from chatbot.pipeline.cancel_scope import CancelScope
from chatbot.pipeline.handler_types import LLMIn, LLMOut
from chatbot.pipeline.messages import (
    EndOfResponse,
    LLMResponseChunk,
    TokenUsage,
    ToolActivity,
)
from chatbot.pipeline.speculative_turns import SpeculativeTurnTracker
from chatbot.utils.utils import is_out_of_band, response_wants_audio

logger = logging.getLogger(__name__)

# About 18–24 seconds of default SDK backoff before warmup fails.
WARMUP_MAX_RETRIES = 6


# ── Normalised provider events ────────────────────────────────────────────────
# Each backend's stream/response is mapped to this small vocabulary so the shared
# speech-pipeline logic (sentence batching, cancellation, history, token usage)
# lives in one place. Subclasses differ only in how they produce these events.


class TextDelta(BaseModel):
    """Incremental assistant text. Always RAW (unfiltered); the base applies
    ``remove_unspeechable`` for the audio path."""

    text: str


class AssistantMessage(BaseModel):
    """A complete assistant turn to write back to history."""

    content: list[AssistantContent]


class ToolCall(BaseModel):
    """A complete function tool call (``call_id`` / ``id`` already regenerated)."""

    item: ResponseFunctionToolCall


class Usage(BaseModel):
    """Token accounting for the turn."""

    input_tokens: int
    output_tokens: int


ProviderEvent = TextDelta | AssistantMessage | ToolCall | Usage
SerializeFn = Callable[[Chat], Any]
RequestFn = Callable[[Any, dict[str, Any]], Any]
EventIteratorFn = Callable[[Any], Iterator[ProviderEvent]]


class _Turn(BaseModel):
    """Per-request context threaded through generation (immutable for the turn)."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    language_code: Optional[str]
    gen: int | None
    runtime_config: Any
    response: Any
    turn_id: str | None
    turn_revision: int | None
    speech_stopped_at_s: float | None
    wants_audio: bool


class _GenState(BaseModel):
    """Mutable accumulators collected while consuming a turn's events."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    # The chat serialised for each model call this response. Items recorded
    # mid-response (assistant text, tool calls, tool outputs) are appended here
    # so the next server-side tool round sees them.
    active_chat: Chat | None = None
    tools: list[ResponseFunctionToolCall] = Field(default_factory=list)
    pending: list[SupportedItem] = Field(default_factory=list)
    recorded_item_ids: set[str] = Field(default_factory=set)
    recorded_call_ids: set[str] = Field(default_factory=set)
    clean_text: str = ""  # filtered text, kept only for the debug log
    input_tokens: int = 0  # summed over every model call in the response
    output_tokens: int = 0


class BaseOpenAICompatibleHandler(BaseHandler[LLMIn, LLMOut], ABC):
    """Shared lifecycle for OpenAI-compatible LLM backends (Responses & Chat
    Completions).

    Subclasses implement four hooks — :meth:`warmup`,
    :meth:`_build_compaction_generate_fn`, :meth:`_serialize`, :meth:`_request`,
    :meth:`_iter_events` and :meth:`_build_optional_kwargs` — and inherit the
    request/response orchestration: speculative-turn gating, cancellation,
    sentence batching, text-only vs audio handling, history write-back, token
    usage, out-of-band handling and error termination.
    """

    # ── setup ─────────────────────────────────────────────────────────────────

    def setup(
        self,
        model_name: str = "gpt-5.4-mini",
        device: str = "cuda",
        gen_kwargs: dict[str, Any] = {},
        base_url: str = "https://openrouter.ai/api/v1",
        api_key: Optional[str] = None,
        stream: bool = True,
        user_role: str = "user",
        cancel_scope: CancelScope | None = None,
        speculative_turns: SpeculativeTurnTracker | None = None,
        disable_thinking: bool = True,
        reasoning_effort: Optional[str] = None,
        request_timeout_s: float = 20.0,
        stream_batch_sentences: int = 3,
        enable_lang_prompt: bool = False,
        compact_history: bool = False,
        sidecar_url: str = "",
        **_kwargs: Any,
    ) -> None:
        self.cancel_scope = cancel_scope
        self.speculative_turns = speculative_turns
        self.model_name = model_name
        self.stream = stream
        self.stream_batch_sentences = max(1, stream_batch_sentences)
        self.enable_lang_prompt = enable_lang_prompt
        self.gen_kwargs = dict(gen_kwargs)
        self.base_url = base_url
        self.request_timeout_s = float(request_timeout_s)
        self.request_timeout = httpx.Timeout(
            self.request_timeout_s,
            connect=min(10.0, self.request_timeout_s),
        )

        self.user_role = user_role
        if api_key is None:
            api_key = os.environ.get("OPENROUTER_API_KEY")
        self.client = OpenAI(api_key=api_key, base_url=base_url)
        self._reasoning_effort = (reasoning_effort or "").strip().lower() or None
        self._extra_body = self._build_extra_body(base_url, disable_thinking, self._reasoning_effort)
        self.compactor = build_compactor(self._build_compaction_generate_fn()) if compact_history else None
        self.server_tools = ServerToolExecutor(sidecar_url) if sidecar_url.strip() else None
        if self.server_tools is not None:
            logger.info("Server-side research tools enabled via %s", self.server_tools.base_url)
        self.warmup()

    # Class-level defaults so partially-initialised handlers (tests build them
    # with object.__new__) still have every attribute _request() reads.
    _reasoning_effort: Optional[str] = None
    _extra_body: Optional[dict[str, Any]] = None
    server_tools: ServerToolExecutor | None = None

    @classmethod
    def _build_extra_body(
        cls,
        base_url: Optional[str],
        disable_thinking: bool,
        reasoning_effort: Optional[str],
    ) -> Optional[dict[str, Any]]:
        """Build the provider-specific ``extra_body`` used to turn reasoning off.

        When a ``reasoning_effort`` is configured it is sent as the standard
        ``reasoning: {effort}`` request parameter (see :meth:`_request`) and no
        extra body is needed. Otherwise, for providers that only understand the
        chat-template flag (vLLM/Qwen), ``disable_thinking`` sends
        ``chat_template_kwargs.enable_thinking=false``.
        """
        if reasoning_effort:
            return None
        if disable_thinking:
            return {"chat_template_kwargs": {"enable_thinking": False}}
        return None

    # ── subclass hooks ──────────────────────────────────────────────────────--

    @abstractmethod
    def warmup(self) -> None:
        """Issue a cheap request so the model/connection is ready before serving."""
        ...

    @abstractmethod
    def _build_compaction_generate_fn(self) -> CompactGenerateFn:
        """Return a ``(system, user) -> text`` fn used to compact long histories."""
        ...

    @abstractmethod
    def _serialize(self, active_chat: Chat) -> Any:
        """Serialise the chat to the backend's request payload (input/messages)."""
        ...

    @abstractmethod
    def _request(self, api_input: Any, optional_kwargs: dict[str, Any]) -> Any:
        """Issue the create() call and return the response or stream."""
        ...

    @abstractmethod
    def _iter_stream_events(self, api_response: Any) -> Iterator[ProviderEvent]:
        """Map a streaming response to normalised :data:`ProviderEvent`s."""
        ...

    @abstractmethod
    def _iter_response_events(self, api_response: Any) -> Iterator[ProviderEvent]:
        """Map a non-streaming response to normalised :data:`ProviderEvent`s."""
        ...

    def _iter_events(self, api_response: Any) -> Iterator[ProviderEvent]:
        """Dispatch to the stream/non-stream mapper. ``self.stream`` is the single
        source of truth (it set the request's ``stream=`` flag), so the response
        type always matches it."""
        if self.stream:
            yield from self._iter_stream_events(api_response)
        else:
            yield from self._iter_response_events(api_response)

    @abstractmethod
    def _build_optional_kwargs(self, req_tools: Any, req_tool_choice: Any) -> dict[str, Any]:
        """Build the per-request tools/tool_choice kwargs in the backend's shape."""
        ...

    # ── speculative-turn / cancellation gating ─────────────────────────────────

    def _generation_is_stale(self, gen: int | None) -> bool:
        return gen is not None and self.cancel_scope is not None and self.cancel_scope.is_stale(gen)

    def _turn_output_allowed(self, turn_id: str | None, turn_revision: int | None) -> bool:
        if self.speculative_turns is None:
            return True
        return self.speculative_turns.is_latest(turn_id, turn_revision)

    def _memory_profile(self) -> str:
        """Personal memory to fold into the system prompt; empty when unavailable."""
        return self.server_tools.memory_profile() if self.server_tools is not None else ""

    def _runs_server_side(self, tool_name: str) -> bool:
        return self.server_tools is not None and self.server_tools.handles(tool_name)

    def _apply_config(
        self,
        chat: Chat,
        instructions: Optional[str],
        wants_audio: bool = True,
    ) -> None:
        if instructions:
            builder = build_voice_system_prompt if wants_audio else build_text_system_prompt
            full_instructions = builder(instructions, memory=self._memory_profile())
            logger.info(
                "Applied %s system prompt (%d chars, conversation_partner=%s)",
                "voice" if wants_audio else "text",
                len(full_instructions),
                "conversation partner" in full_instructions.lower(),
            )
            chat.add_item(make_system_message(full_instructions))

    # ── output helpers ──────────────────────────────────────────────────────--

    def _chunk(
        self,
        turn: _Turn,
        *,
        text: str = "",
        tools: list[ResponseFunctionToolCall] | None = None,
        language_code: Optional[str] = None,
    ) -> LLMResponseChunk:
        return LLMResponseChunk(
            text=text,
            language_code=language_code if language_code is not None else turn.language_code,
            tools=tools or [],
            runtime_config=turn.runtime_config,
            response=turn.response,
            turn_id=turn.turn_id,
            turn_revision=turn.turn_revision,
            speech_stopped_at_s=turn.speech_stopped_at_s,
            cancel_generation=turn.gen,
        )

    def _record_items(self, state: _GenState, turn: _Turn, items: list[SupportedItem]) -> None:
        """Append items to the chat the model reads next round and, for in-band
        turns, to the default conversation.

        ``state.active_chat`` is the per-response working copy: server-side tool
        rounds re-serialise it, so it must see assistant text, function calls and
        their outputs as they happen. Out-of-band turns stop there (their context
        is a throwaway); in-band turns also persist to the default conversation
        so a client ``function_call_output`` can pair with its call."""
        for item in items:
            recorded = state.active_chat.add_item(item) if state.active_chat is not None else item
            if not is_out_of_band(turn.response):
                recorded = turn.runtime_config.chat.add_item(item)
            if recorded.id is not None:
                state.recorded_item_ids.add(recorded.id)
            if (
                isinstance(recorded, (RealtimeConversationItemFunctionCall, RealtimeConversationItemFunctionCallOutput))
                and recorded.call_id
            ):
                state.recorded_call_ids.add(recorded.call_id)

    def _record_tool_call(self, state: _GenState, turn: _Turn, item: ResponseFunctionToolCall) -> Iterator[LLMOut]:
        """Persist a tool call (and any assistant text seen so far) and announce it.

        The function_call must already exist in the conversation by the time the
        client returns its ``function_call_output``; otherwise a fast client
        races ahead of the deferred end-of-turn write-back and the output is
        rejected ("No function_call with call_id ... found"), which makes the
        model re-issue the same tool call. The call lands in ``_pending_tool_calls``
        (not serialized until its output pairs it), so eager recording is safe.

        Tools the server runs itself are announced as :class:`ToolActivity`
        (the client shows them, it does not execute them); everything else is
        forwarded in a chunk for the client to run. A stale turn records nothing."""
        state.tools.append(item)
        fc_item = RealtimeConversationItemFunctionCall(
            type="function_call",
            name=item.name,
            arguments=item.arguments,
            call_id=item.call_id,
            id=item.id,
            status=item.status,
        )
        if self._generation_is_stale(turn.gen) or not self._turn_output_allowed(turn.turn_id, turn.turn_revision):
            logger.info("LLM generation cancelled (stale speculative turn)")
            return
        # Flush assistant text accumulated before this call first (so history
        # order matches what the client received), then persist the call —
        # all before anything leaves for the client.
        self._record_items(state, turn, [*state.pending, fc_item])
        state.pending.clear()
        if self._runs_server_side(item.name):
            yield ToolActivity(
                status="started",
                call_id=item.call_id,
                item_id=fc_item.id or "",
                name=item.name,
                arguments=item.arguments or "{}",
                turn_id=turn.turn_id,
                turn_revision=turn.turn_revision,
                cancel_generation=turn.gen,
            )
            return
        yield self._chunk(turn, tools=[item])

    def _run_server_tools(
        self,
        calls: list[ResponseFunctionToolCall],
        state: _GenState,
        turn: _Turn,
    ) -> Generator[LLMOut, None, bool]:
        """Execute this round's server-side calls, append their outputs, report them.

        Returns False when the turn was interrupted while tools were running; the
        outputs are then dropped and the response ends without another model call."""
        assert self.server_tools is not None

        def interrupted() -> bool:
            return self._generation_is_stale(turn.gen) or not self._turn_output_allowed(
                turn.turn_id, turn.turn_revision
            )

        outputs = self.server_tools.run_many(calls, is_cancelled=interrupted)
        if any(output is None for output in outputs) or interrupted():
            logger.info("LLM generation cancelled while tools were running")
            return False
        for call, output in zip(calls, outputs, strict=True):
            assert output is not None
            output_item = RealtimeConversationItemFunctionCallOutput(
                type="function_call_output",
                call_id=call.call_id,
                output=output,
            )
            self._record_items(state, turn, [output_item])
            yield ToolActivity(
                status="finished",
                call_id=call.call_id,
                item_id=output_item.id or "",
                name=call.name,
                arguments=call.arguments or "{}",
                output=output,
                turn_id=turn.turn_id,
                turn_revision=turn.turn_revision,
                cancel_generation=turn.gen,
            )
        return True

    # ── consumption ─────────────────────────────────────────────────────────--

    def _consume_streaming(
        self,
        events: Iterator[ProviderEvent],
        state: _GenState,
        turn: _Turn,
    ) -> Generator[LLMOut, None, bool]:
        cancelled = False
        printable_text = ""
        sentence_batch: list[str] = []
        # The first complete sentence goes to TTS on its own so speech starts as
        # early as possible; later sentences are batched (stream_batch_sentences)
        # for better prosody once audio is already playing.
        first_flush_pending = True

        def _flush(batch: list[str]) -> Iterator[LLMOut]:
            if not batch:
                return
            if not self._turn_output_allowed(turn.turn_id, turn.turn_revision):
                logger.info("LLM generation cancelled (stale speculative turn)")
                return
            yield self._chunk(turn, text=" ".join(batch))

        for event in events:
            if self._generation_is_stale(turn.gen) or not self._turn_output_allowed(turn.turn_id, turn.turn_revision):
                logger.info("LLM generation cancelled (interruption)")
                cancelled = True
                break

            if isinstance(event, Usage):
                state.input_tokens += event.input_tokens
                state.output_tokens += event.output_tokens
            elif isinstance(event, AssistantMessage):
                state.pending.append(
                    RealtimeConversationItemAssistantMessage(type="message", role="assistant", content=event.content)
                )
            elif isinstance(event, ToolCall):
                # Flush any pending spoken text before emitting the tool call.
                if printable_text.strip():
                    sentence_batch.append(printable_text.strip())
                    printable_text = ""
                if sentence_batch:
                    if not self._turn_output_allowed(turn.turn_id, turn.turn_revision):
                        logger.info("LLM generation cancelled (stale speculative turn)")
                        cancelled = True
                        break
                    yield from _flush(sentence_batch)
                    sentence_batch = []
                yield from self._record_tool_call(state, turn, event.item)
            elif isinstance(event, TextDelta):
                if not turn.wants_audio:
                    # Text-only: forward verbatim. Keep every character (no
                    # remove_unspeechable, which strips TTS-unfriendly symbols) and
                    # don't sentence-split (sent_tokenize collapses newlines/markdown).
                    state.clean_text += event.text
                    if event.text:
                        if not self._turn_output_allowed(turn.turn_id, turn.turn_revision):
                            logger.info("LLM generation cancelled (stale speculative turn)")
                            cancelled = True
                            break
                        yield self._chunk(turn, text=event.text)
                    continue
                new_text = remove_unspeechable(event.text)
                state.clean_text += new_text
                printable_text += new_text
                sentences = sent_tokenize(printable_text)
                if len(sentences) > 1:
                    for s in sentences[:-1]:
                        sentence_batch.append(s)
                        threshold = 1 if first_flush_pending else self.stream_batch_sentences
                        if len(sentence_batch) >= threshold:
                            if not self._turn_output_allowed(turn.turn_id, turn.turn_revision):
                                logger.info("LLM generation cancelled (stale speculative turn)")
                                cancelled = True
                                break
                            yield from _flush(sentence_batch)
                            sentence_batch = []
                            first_flush_pending = False
                    if cancelled:
                        break
                    # Keep the raw tail (sent_tokenize strips its trailing space)
                    # so the next delta cannot glue onto it as "one.Here" and
                    # hide a sentence boundary from the tokenizer.
                    tail_start = printable_text.rfind(sentences[-1])
                    printable_text = printable_text[tail_start:] if tail_start >= 0 else sentences[-1]

        if not cancelled:
            if printable_text.strip():
                sentence_batch.append(printable_text.strip())
            if sentence_batch:
                if self._generation_is_stale(turn.gen):
                    logger.info("LLM generation cancelled (interruption)")
                else:
                    logger.debug(f"Clean text: {state.clean_text}")
                    yield from _flush(sentence_batch)
            logger.info(f"Tools: {state.tools}")
        return (
            not cancelled
            and not self._generation_is_stale(turn.gen)
            and self._turn_output_allowed(turn.turn_id, turn.turn_revision)
        )

    def _consume_nonstreaming(
        self,
        events: Iterator[ProviderEvent],
        state: _GenState,
        turn: _Turn,
    ) -> Generator[LLMOut, None, bool]:
        if self._generation_is_stale(turn.gen) or not self._turn_output_allowed(turn.turn_id, turn.turn_revision):
            logger.info("LLM generation cancelled (interruption)")
            return False
        for event in events:
            if isinstance(event, Usage):
                state.input_tokens += event.input_tokens
                state.output_tokens += event.output_tokens
            elif isinstance(event, AssistantMessage):
                state.pending.append(
                    RealtimeConversationItemAssistantMessage(type="message", role="assistant", content=event.content)
                )
            elif isinstance(event, ToolCall):
                yield from self._record_tool_call(state, turn, event.item)
            elif isinstance(event, TextDelta):
                # Text-only keeps every character verbatim; audio strips
                # TTS-unfriendly symbols via remove_unspeechable.
                spoken = event.text if not turn.wants_audio else remove_unspeechable(event.text)
                state.clean_text += spoken
                out = spoken if not turn.wants_audio else spoken.strip()
                if (
                    out
                    and not self._generation_is_stale(turn.gen)
                    and self._turn_output_allowed(turn.turn_id, turn.turn_revision)
                ):
                    yield self._chunk(turn, text=out)
        logger.debug(f"Clean text: {state.clean_text}")
        logger.info(f"Tools: {state.tools}")
        return not self._generation_is_stale(turn.gen) and self._turn_output_allowed(turn.turn_id, turn.turn_revision)

    # ── orchestration ─────────────────────────────────────────────────────────

    def _generate(
        self,
        active_chat: Chat,
        original_chat: Chat,
        turn: _Turn,
        optional_kwargs: dict[str, Any],
        *,
        serialize_fn: SerializeFn | None = None,
        request_fn: RequestFn | None = None,
        event_iterator_fn: EventIteratorFn | None = None,
        transactional_user_message_id: str | None = None,
        history_commit_fn: Callable[[], None] | None = None,
    ) -> Generator[LLMOut, None, bool]:
        api_response: Any = None
        state = _GenState(active_chat=active_chat)
        error_message: str | None = None
        generation_completed = False
        history_committed = False
        transaction_rolled_back = False
        consumed_image_ids: set[str] = set()

        def rollback_transaction() -> None:
            nonlocal transaction_rolled_back
            if transactional_user_message_id is None or history_committed or transaction_rolled_back:
                return
            original_chat.rollback_generation(
                transactional_user_message_id,
                item_ids=state.recorded_item_ids,
                call_ids=state.recorded_call_ids,
            )
            transaction_rolled_back = True

        try:
            try:
                # One response may take several model calls: each round streams text
                # (spoken as it arrives) and may end in tool calls. Tools the server
                # runs itself are executed here and the loop continues with their
                # outputs in context; a tool the client must run ends the loop (the
                # client posts its output and asks for a new response).
                research_started = time.monotonic()
                for tool_round in range(MAX_TOOL_ROUNDS + 1):
                    api_input = (serialize_fn or self._serialize)(active_chat)
                    # Images the model actually sees this turn; only these are stripped on
                    # write-back, so an image a fast client injects mid-generation for the
                    # next turn survives (it is not in this serialized snapshot).
                    consumed_image_ids |= active_chat.image_message_ids()
                    if not api_input:
                        # Nothing to send: empty `instructions` and no `input` (in the response,
                        # the default conversation, or the out-of-band context). The provider
                        # would reject this; fail with a clear message instead of an opaque error.
                        error_message = "Cannot generate a response: no instructions and no input were provided."
                        break
                    round_kwargs = optional_kwargs
                    research_s = time.monotonic() - research_started
                    out_of_rounds = tool_round == MAX_TOOL_ROUNDS
                    out_of_time = tool_round > 0 and research_s > TOOL_TIME_BUDGET_S
                    if (out_of_rounds or out_of_time) and "tools" in optional_kwargs:
                        # Out of rounds or time: the model must answer with what it has.
                        logger.warning(
                            "Research budget reached (round %d, %.1fs); forcing a final answer", tool_round, research_s
                        )
                        round_kwargs = {**optional_kwargs, "tool_choice": "none"}
                    api_response = (request_fn or self._request)(api_input, round_kwargs)
                    if api_response is None:
                        break
                    events = (event_iterator_fn or self._iter_events)(api_response)
                    first_new_tool = len(state.tools)
                    if self.stream:
                        generation_completed = yield from self._consume_streaming(events, state, turn)
                    else:
                        generation_completed = yield from self._consume_nonstreaming(events, state, turn)
                    self._close_response(api_response)
                    api_response = None
                    if not generation_completed:
                        break
                    new_tools = state.tools[first_new_tool:]
                    server_calls = [call for call in new_tools if self._runs_server_side(call.name)]
                    client_calls = [call for call in new_tools if not self._runs_server_side(call.name)]
                    if server_calls:
                        logger.info(
                            "Tool round %d: running %s server-side",
                            tool_round + 1,
                            ", ".join(call.name for call in server_calls),
                        )
                        generation_completed = yield from self._run_server_tools(server_calls, state, turn)
                        if not generation_completed:
                            break
                    # A screenshot / code_agent in the same round still has to
                    # finish on the client before the model can continue.
                    if client_calls or not server_calls:
                        break
            except httpx.ReadTimeout:
                logger.warning(
                    "OpenAI API read timed out after %.1fs; ending the current response",
                    self.request_timeout_s,
                )
                if not self._generation_is_stale(turn.gen) and self._turn_output_allowed(
                    turn.turn_id, turn.turn_revision
                ):
                    # Canned apology carries no language_code (mirrors the prior handlers).
                    yield LLMResponseChunk(
                        text="Wow I'm a bit slow today, could you repeat that?",
                        runtime_config=turn.runtime_config,
                        response=turn.response,
                        turn_id=turn.turn_id,
                        turn_revision=turn.turn_revision,
                        speech_stopped_at_s=turn.speech_stopped_at_s,
                        cancel_generation=turn.gen,
                    )
            except Exception as exc:
                # Any other generation failure must still terminate the response: record
                # the error and fall through to the EndOfResponse below. Without this the
                # exception would escape process() and no EndOfResponse would be emitted,
                # leaving st.in_response stuck and locking every subsequent response.
                logger.exception("LLM generation failed; ending the current response")
                if error_message is None:
                    error_message = f"Language model generation failed: {exc}"

            can_commit = (
                error_message is None
                and generation_completed
                and not self._generation_is_stale(turn.gen)
                and self._turn_output_allowed(turn.turn_id, turn.turn_revision)
            )
            if can_commit:
                try:
                    # Out-of-band responses emit output and usage but never write back to the
                    # default conversation (their context was a throwaway chat).
                    if not is_out_of_band(turn.response):
                        # Tool calls (and any assistant text preceding them) were already
                        # written eagerly in _record_tool_call; only trailing items remain.
                        for item in state.pending:
                            recorded = original_chat.add_item(item)
                            if recorded.id is not None:
                                state.recorded_item_ids.add(recorded.id)
                        original_chat.strip_images(consumed_image_ids)
                        if history_commit_fn is not None:
                            history_commit_fn()
                        original_chat.trim_if_needed(self.compactor)
                    history_committed = True
                except Exception as exc:
                    logger.exception("LLM history commit failed; rolling back the current response")
                    error_message = f"Language model history commit failed: {exc}"

            rollback_transaction()
            if history_committed and (state.input_tokens or state.output_tokens):
                yield TokenUsage(
                    input_tokens=state.input_tokens,
                    output_tokens=state.output_tokens,
                    turn_id=turn.turn_id,
                    turn_revision=turn.turn_revision,
                )
            yield EndOfResponse(
                turn_id=turn.turn_id,
                turn_revision=turn.turn_revision,
                cancel_generation=turn.gen,
                error=error_message,
            )
            return history_committed
        finally:
            self._close_response(api_response)
            rollback_transaction()

    @staticmethod
    def _close_response(api_response: Any) -> None:
        if api_response is not None and hasattr(api_response, "close"):
            try:
                api_response.close()
            except Exception:
                pass

    def process(self, request: LLMIn) -> Iterator[LLMOut]:
        """Process a language model request and yield LLMResponseChunks."""
        runtime_config = request.runtime_config
        response = request.response
        turn_id = request.turn_id
        turn_revision = request.turn_revision
        speech_stopped_at_s = request.speech_stopped_at_s
        if not self._turn_output_allowed(turn_id, turn_revision):
            logger.info("Skipping stale LLM request for turn=%s rev=%s", turn_id, turn_revision)
            yield EndOfResponse(
                turn_id=turn_id,
                turn_revision=turn_revision,
                cancel_generation=self.cancel_scope.generation if self.cancel_scope else None,
            )
            return

        original_chat = runtime_config.chat
        if is_out_of_band(response):
            try:
                active_chat = build_active_chat(original_chat, response)
            except ChatItemError as exc:
                logger.info("Out-of-band response rejected: %s", exc)
                yield EndOfResponse(
                    turn_id=turn_id,
                    turn_revision=turn_revision,
                    cancel_generation=self.cancel_scope.generation if self.cancel_scope else None,
                    error=str(exc),
                )
                return
        else:
            active_chat = original_chat.copy()
        language_code = request.language_code
        instructions = (
            response.instructions if response and response.instructions else runtime_config.session.instructions
        ) or ""
        req_tools = response.tools if response and response.tools else runtime_config.session.tools
        req_tool_choice = (
            response.tool_choice if response and response.tool_choice else runtime_config.session.tool_choice
        )
        wants_audio = response_wants_audio(response)
        self._apply_config(active_chat, instructions, wants_audio)
        language_code, lang_name = resolve_auto_language(language_code)
        if lang_name and self.enable_lang_prompt:
            active_chat.add_item(make_user_message(f"Please reply to my message in {lang_name}."))

        optional_kwargs = self._build_optional_kwargs(req_tools, req_tool_choice)

        # CancelScope.is_stale(gen) is checked when the stream iterator advances; a
        # blocked read inside httpx cannot be aborted by cancel_scope.cancel() from
        # the websocket router. Mitigations: request_timeout_s / ReadTimeout.
        gen = self.cancel_scope.generation if self.cancel_scope else None

        turn = _Turn(
            language_code=language_code,
            gen=gen,
            runtime_config=runtime_config,
            response=response,
            turn_id=turn_id,
            turn_revision=turn_revision,
            speech_stopped_at_s=speech_stopped_at_s,
            wants_audio=wants_audio,
        )
        yield from self._generate(active_chat, original_chat, turn, optional_kwargs)

    @property
    def timing_log_level(self) -> int:
        return logging.INFO

    def should_log_timing(self, output: LLMOut) -> bool:
        return isinstance(output, LLMResponseChunk) and self.last_time > self.min_time_to_debug
