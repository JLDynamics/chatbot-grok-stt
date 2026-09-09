"""Research tools the language model runs itself, inside one response.

Every function call used to leave the server: the client received it, called
the sidecar, posted the output back and asked for a new response, then armed
a watchdog in case any of those hops was lost. For tools that only need the
local sidecar HTTP API (web search, page reading, chat history, personal
memory) none of that is necessary. The LLM handler calls the sidecar directly,
appends the output to the conversation and continues the same response, so
"let me check" is followed by the answer with no client round trip.

Only tools that need the app process stay client-side: ``screenshot`` (Screen
Recording permission is per code identity) and ``code_agent`` (runs for
minutes and must not block the pipeline thread).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

import httpx
from openai.types.responses import ResponseFunctionToolCall

logger = logging.getLogger(__name__)

# Tools executed here. Anything else the model calls is forwarded to the client.
# ``web_fetch`` and ``read_article`` are no longer published; they stay as
# aliases of ``read_page`` because replayed history still mentions them.
SERVER_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "web_search",
        "web_fetch",
        "read_page",
        "read_article",
        "search_chat_history",
        "remember",
        "forget",
    }
)

# One response may chain this many tool rounds (search → read → read → answer
# is four). The cap exists so a confused model cannot loop forever in silence.
MAX_TOOL_ROUNDS = 6
# Longest single tool the sidecar runs: read_page may try both rungs
# (15 s fetch + bridge) so the client timeout must sit outside that.
TOOL_TIMEOUT_S = 45.0
# How often the waiting loop re-checks for a barge-in while a tool runs.
CANCEL_POLL_S = 0.1
# Personal memory is re-read from the sidecar (a sub-millisecond local call)
# at most this often, so an edit made in Settings is live by the next turn
# while a burst of turns does not hammer the sidecar.
MEMORY_CACHE_TTL_S = 5.0
# Page text longer than this is cut for the model; the sidecar already caps
# fetches, this only guards the Chrome bridge path (up to 60k chars).
PAGE_TEXT_MAX_CHARS = 16_000
SEARCH_RESULTS_SHOWN = 5

ToolOutput = str


class ServerToolExecutor:
    """Runs research tools against the local sidecar (``web_app/server.py``).

    Thread-safe: one instance per LLM handler, called from the handler's
    pipeline thread; tool HTTP calls run on a small pool so a round with
    several calls (``parallel_tool_calls``) finishes in the time of the slowest.
    """

    def __init__(
        self,
        sidecar_url: str,
        *,
        timeout_s: float = TOOL_TIMEOUT_S,
        max_workers: int = 4,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = sidecar_url.rstrip("/")
        self._client = client or httpx.Client(
            base_url=self.base_url,
            timeout=httpx.Timeout(timeout_s, connect=3.0),
            limits=httpx.Limits(max_keepalive_connections=4, max_connections=8),
        )
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="server-tool")
        self._memory_lock = threading.Lock()
        self._memory_cached_at = 0.0
        self._memory_text = ""

    # ── public API ───────────────────────────────────────────────────────────

    @staticmethod
    def handles(name: str) -> bool:
        return name in SERVER_TOOL_NAMES

    def run(self, name: str, arguments_json: str) -> ToolOutput:
        """Execute one tool and return the text the model reads as its output."""
        try:
            arguments = json.loads(arguments_json or "{}")
        except json.JSONDecodeError:
            arguments = {}
        if not isinstance(arguments, dict):
            arguments = {}
        started = time.perf_counter()
        try:
            output = self._dispatch(name, arguments)
        except httpx.HTTPStatusError as exc:
            output = f"{name} failed: {_error_detail(exc.response)}"
        except httpx.TimeoutException:
            output = f"{name} timed out after {TOOL_TIMEOUT_S:.0f}s. Tell the user it did not finish."
        except httpx.HTTPError as exc:
            output = (
                f"{name} is unavailable: the local research service could not be reached ({exc.__class__.__name__})."
                " Answer from what you know and say you could not check."
            )
        except Exception:  # noqa: BLE001 - a tool bug must not kill the response
            logger.exception("Server tool %s crashed", name)
            output = f"{name} failed unexpectedly. Answer from what you know and say you could not check."
        logger.info("Server tool %s finished in %.2fs (%d chars)", name, time.perf_counter() - started, len(output))
        return output

    def run_many(
        self,
        calls: Sequence[ResponseFunctionToolCall],
        *,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> list[ToolOutput | None]:
        """Run several calls concurrently.

        Returns one output per call, in order. When ``is_cancelled`` turns true
        (the user barged in) the wait stops and every unfinished slot is
        ``None``; the HTTP requests finish on their own in the pool and are
        discarded.
        """
        futures: list[Future[ToolOutput]] = [
            self._pool.submit(self.run, call.name, call.arguments or "{}") for call in calls
        ]
        outputs: list[ToolOutput | None] = [None] * len(futures)
        pending = set(range(len(futures)))
        while pending:
            if is_cancelled is not None and is_cancelled():
                logger.info("Abandoning %d running tool call(s): the turn was interrupted", len(pending))
                break
            for index in list(pending):
                future = futures[index]
                if future.done():
                    outputs[index] = future.result()
                    pending.discard(index)
            if pending:
                time.sleep(CANCEL_POLL_S)
        return outputs

    def memory_profile(self) -> str:
        """The user's personal profile, cached briefly; empty when unavailable."""
        with self._memory_lock:
            if time.monotonic() - self._memory_cached_at < MEMORY_CACHE_TTL_S:
                return self._memory_text
        text = ""
        try:
            response = self._client.get("/personal-memory")
            response.raise_for_status()
            text = str(response.json().get("content") or "").strip()
        except Exception as exc:  # noqa: BLE001 - memory is best effort
            logger.warning("Personal memory unavailable: %s", exc)
        with self._memory_lock:
            self._memory_text = text
            self._memory_cached_at = time.monotonic()
        return text

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
        self._client.close()

    # ── dispatch ─────────────────────────────────────────────────────────────

    def _dispatch(self, name: str, arguments: dict[str, Any]) -> ToolOutput:
        if name == "web_search":
            return self._web_search(str(arguments.get("query") or ""))
        if name == "read_page":
            return self._read_page(arguments.get("url"), bool(arguments.get("prefer_browser", False)))
        if name == "web_fetch":
            return self._read_page(arguments.get("url"), False)
        if name == "read_article":
            return self._read_page(arguments.get("url"), True)
        if name == "search_chat_history":
            return self._search_chat_history(str(arguments.get("query") or ""))
        if name == "remember":
            return self._memory_edit("/personal-memory/remember", {"fact": str(arguments.get("fact") or "")})
        if name == "forget":
            return self._memory_edit("/personal-memory/forget", {"memory": str(arguments.get("memory") or "")})
        return f"Unknown tool: {name}"

    def _web_search(self, query: str) -> ToolOutput:
        query = query.strip()
        if not query:
            return "No search query provided."
        response = self._client.post("/search", json={"query": query})
        response.raise_for_status()
        return format_search_results(response.json())

    def _read_page(self, url: Any, prefer_browser: bool) -> ToolOutput:
        cleaned = str(url or "").strip() or None
        response = self._client.post("/read_page", json={"url": cleaned, "prefer_browser": prefer_browser})
        response.raise_for_status()
        return format_page_result(response.json())

    def _search_chat_history(self, query: str) -> ToolOutput:
        query = query.strip()
        if not query:
            return "No search query provided."
        response = self._client.get("/history/search", params={"q": query, "limit": 8})
        response.raise_for_status()
        results = response.json().get("results") or []
        if not results:
            return "No matching saved conversation was found."
        return "\n\n".join(
            f"[{item.get('title', '')}] {item.get('role', '')}: {str(item.get('text', '')).strip()}" for item in results
        )

    def _memory_edit(self, path: str, payload: dict[str, str]) -> ToolOutput:
        response = self._client.post(path, json=payload)
        if response.status_code == 400:
            return _error_detail(response)
        response.raise_for_status()
        with self._memory_lock:
            self._memory_cached_at = 0.0  # next turn re-reads the profile
        return str(response.json().get("message") or "Done.")


# ── model-facing formatting ──────────────────────────────────────────────────


def format_search_results(data: dict[str, Any]) -> ToolOutput:
    lines: list[str] = []
    answer = str(data.get("answer") or "").strip()
    if answer:
        lines.append(f"Direct answer: {answer}")
    for index, item in enumerate((data.get("results") or [])[:SEARCH_RESULTS_SHOWN], start=1):
        title = str(item.get("title") or "").strip()
        snippet = str(item.get("content") or item.get("snippet") or "").strip()
        link = str(item.get("url") or "").strip()
        lines.append(f"[{index}] {title}\n{snippet}\nURL: {link}")
    return "\n\n".join(lines) if lines else "No search results found."


def format_page_result(data: dict[str, Any]) -> ToolOutput:
    """Turn a ``/api/read_page`` result into compact text for the model.

    A header names the outcome and where the text came from; the attempts
    line explains any rung that failed so the model can say so honestly.
    """
    status = str(data.get("status") or "unavailable")
    attempts = data.get("attempts") or []
    tried = "; ".join(
        f"{attempt.get('method')}: {attempt.get('status')}"
        + (f" ({attempt['reason']})" if attempt.get("reason") else "")
        for attempt in attempts
        if isinstance(attempt, dict)
    )
    url = str(data.get("url") or "").strip()
    title = str(data.get("title") or "").strip()
    text = str(data.get("text") or "").strip()
    if status == "unavailable" or not text:
        where = f" {url}" if url else ""
        message = str(data.get("message") or "The page could not be read.").strip()
        parts = [f"Could not read the page{where}."]
        if tried:
            parts.append(f"Tried {tried}.")
        parts.append(message)
        return " ".join(parts)
    source = str(data.get("source") or "unknown")
    header = [f"status: {status} (source: {source})"]
    if title:
        header.append(f"title: {title}")
    if url:
        header.append(f"url: {url}")
    if status == "partial":
        header.append("note: only part of the page was readable; say so if it matters.")
    if len(attempts) > 1 and tried:
        header.append(f"attempts: {tried}")
    if len(text) > PAGE_TEXT_MAX_CHARS:
        text = text[:PAGE_TEXT_MAX_CHARS].rstrip() + "\n[... text cut here ...]"
    return "\n".join(header) + "\n\n" + text


def _error_detail(response: httpx.Response) -> str:
    try:
        detail = response.json().get("detail")
    except Exception:  # noqa: BLE001 - non-JSON error bodies
        detail = None
    if isinstance(detail, str) and detail:
        return detail
    if isinstance(detail, dict):
        message = detail.get("message")
        if isinstance(message, str) and message:
            return message
        return json.dumps(detail, sort_keys=True)
    return f"HTTP {response.status_code}"
