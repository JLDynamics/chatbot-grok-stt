"""ServerToolExecutor: the research tools the LLM handler runs against the sidecar."""

from __future__ import annotations

import json
import threading
import time

import httpx
from openai.types.responses import ResponseFunctionToolCall

from chatbot.LLM import server_tools
from chatbot.LLM.server_tools import ServerToolExecutor, format_page_result, format_search_results


def _executor(handler, **kwargs) -> ServerToolExecutor:
    client = httpx.Client(base_url="http://sidecar.test/api", transport=httpx.MockTransport(handler))
    return ServerToolExecutor("http://sidecar.test/api", client=client, **kwargs)


def _call(name: str, **arguments) -> ResponseFunctionToolCall:
    return ResponseFunctionToolCall(
        type="function_call", call_id=f"call_{name}", name=name, arguments=json.dumps(arguments)
    )


def test_handles_only_the_research_tools():
    assert ServerToolExecutor.handles("web_search")
    assert ServerToolExecutor.handles("read_page")
    assert ServerToolExecutor.handles("remember")
    assert not ServerToolExecutor.handles("screenshot")
    assert not ServerToolExecutor.handles("code_agent")


def test_web_search_posts_query_and_formats_results():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "answer": "42",
                "results": [
                    {"title": "Example", "snippet": "A snippet", "url": "https://example.com"},
                    {"title": "Second", "content": "Tavily content", "url": "https://two.example"},
                ],
            },
        )

    output = _executor(handler).run("web_search", '{"query": "meaning of life"}')
    assert seen[0].url.path == "/api/search"
    assert json.loads(seen[0].content) == {"query": "meaning of life"}
    assert output.startswith("Direct answer: 42")
    assert "[1] Example\nA snippet\nURL: https://example.com" in output
    assert "[2] Second\nTavily content\nURL: https://two.example" in output


def test_web_search_reuses_a_fresh_result_instead_of_calling_again():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={"results": [{"title": "Once", "snippet": "Only once", "url": "https://example.com"}]},
        )

    executor = _executor(handler)
    first = executor.run("web_search", '{"query": "Paris weather"}')
    second = executor.run("web_search", '{"query": "PARIS WEATHER"}')
    assert len(seen) == 1
    assert first == second


def test_web_search_without_query_does_not_hit_the_sidecar():
    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request expected")

    assert _executor(handler).run("web_search", "{}") == "No search query provided."


def test_read_page_and_aliases_map_onto_the_ladder_endpoint():
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/read_page"
        seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "status": "read",
                "source": "web_fetch",
                "url": "https://example.com/a",
                "title": "Article",
                "text": "Body text.",
                "complete": True,
                "attempts": [{"method": "web_fetch", "status": "read", "reason": ""}],
            },
        )

    executor = _executor(handler)
    executor.run("read_page", '{"url": "https://example.com/a", "prefer_browser": true}')
    executor.run("web_fetch", '{"url": "https://example.com/a"}')
    executor.run("read_article", "{}")
    assert seen == [
        {"url": "https://example.com/a", "prefer_browser": True},
        {"url": "https://example.com/a", "prefer_browser": False},
        {"url": None, "prefer_browser": True},
    ]


def test_format_page_result_read_partial_and_unavailable():
    read = format_page_result(
        {
            "status": "read",
            "source": "chrome_bridge",
            "url": "https://example.com/a",
            "title": "Article",
            "text": "Body text.",
            "attempts": [
                {"method": "web_fetch", "status": "blocked", "reason": "paywall_or_interstitial"},
                {"method": "chrome_bridge", "status": "read", "reason": ""},
            ],
        }
    )
    assert read.splitlines()[0] == "status: read (source: chrome_bridge)"
    assert "title: Article" in read
    assert "attempts: web_fetch: blocked (paywall_or_interstitial); chrome_bridge: read" in read
    assert read.endswith("\n\nBody text.")

    partial = format_page_result({"status": "partial", "source": "web_fetch", "text": "Half.", "attempts": []})
    assert "only part of the page was readable" in partial

    unavailable = format_page_result(
        {
            "status": "unavailable",
            "url": "https://example.com/a",
            "message": "Click the toolbar icon.",
            "attempts": [
                {"method": "web_fetch", "status": "failed", "reason": "Could not fetch that page."},
                {"method": "chrome_bridge", "status": "failed", "reason": "bridge_never_enabled"},
            ],
        }
    )
    assert unavailable == (
        "Could not read the page https://example.com/a. "
        "Tried web_fetch: failed (Could not fetch that page.); chrome_bridge: failed (bridge_never_enabled). "
        "Click the toolbar icon."
    )


def test_format_page_result_caps_very_long_text(monkeypatch):
    monkeypatch.setattr(server_tools, "PAGE_TEXT_MAX_CHARS", 20)
    output = format_page_result({"status": "read", "source": "web_fetch", "text": "y" * 100, "attempts": []})
    assert output.endswith("[... text cut here ...]")
    assert output.count("y") == 20


def test_format_search_results_empty():
    assert format_search_results({"results": []}) == "No search results found."


def test_search_chat_history_formats_excerpts():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/history/search"
        assert request.url.params["q"] == "kitchen remodel"
        return httpx.Response(
            200,
            json={"results": [{"title": "Home", "role": "user", "text": "We said the kitchen remodel starts in May."}]},
        )

    output = _executor(handler).run("search_chat_history", '{"query": "kitchen remodel"}')
    assert output == "[Home] user: We said the kitchen remodel starts in May."


def test_remember_and_forget_invalidate_the_memory_cache():
    profile = {"content": "- Jack likes tea\n"}
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/api/personal-memory":
            return httpx.Response(200, json=profile)
        if request.url.path == "/api/personal-memory/remember":
            profile["content"] += "- Jack works at Costco\n"
            return httpx.Response(200, json={"ok": True, "message": "Saved to the personal profile."})
        if request.url.path == "/api/personal-memory/forget":
            return httpx.Response(200, json={"ok": True, "message": "Removed it from the personal profile."})
        raise AssertionError(request.url.path)

    executor = _executor(handler)
    assert executor.memory_profile() == "- Jack likes tea"
    assert executor.memory_profile() == "- Jack likes tea"  # cached
    assert calls.count("/api/personal-memory") == 1

    assert executor.run("remember", '{"fact": "Jack works at Costco"}') == "Saved to the personal profile."
    assert "Costco" in executor.memory_profile()  # cache dropped by the edit
    assert calls.count("/api/personal-memory") == 2

    assert executor.run("forget", '{"memory": "tea"}') == "Removed it from the personal profile."


def test_sidecar_400_detail_is_returned_as_the_tool_output():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"detail": "Provide at least three characters."})

    assert _executor(handler).run("forget", '{"memory": "x"}') == "Provide at least three characters."


def test_sidecar_error_status_is_explained_to_the_model():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "Search is not configured."})

    assert _executor(handler).run("web_search", '{"query": "x"}') == "web_search failed: Search is not configured."


def test_unreachable_sidecar_tells_the_model_to_answer_without_checking():
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    output = _executor(handler).run("web_search", '{"query": "x"}')
    assert output.startswith("web_search is unavailable")
    assert "could not check" in output


def test_timeout_is_reported_not_raised():
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    output = _executor(handler).run("read_page", '{"url": "https://slow.example"}')
    assert output.startswith("read_page timed out")


def test_malformed_arguments_are_tolerated():
    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request expected")

    assert _executor(handler).run("web_search", "not json") == "No search query provided."
    assert _executor(handler).run("mystery", "{}") == "Unknown tool: mystery"


def test_run_many_executes_calls_concurrently_in_order():
    started = threading.Barrier(2, timeout=5)

    def handler(request: httpx.Request) -> httpx.Response:
        query = json.loads(request.content)["query"]
        started.wait()  # both requests must be in flight together
        return httpx.Response(200, json={"results": [{"title": query, "snippet": "", "url": ""}]})

    outputs = _executor(handler).run_many([_call("web_search", query="one"), _call("web_search", query="two")])
    assert [output.split("\n")[0] for output in outputs] == ["[1] one", "[1] two"]


def test_run_many_abandons_slow_calls_when_the_turn_is_interrupted(monkeypatch):
    monkeypatch.setattr(server_tools, "CANCEL_POLL_S", 0.01)
    release = threading.Event()

    def handler(_request: httpx.Request) -> httpx.Response:
        release.wait(timeout=5)
        return httpx.Response(200, json={"results": []})

    cancelled = threading.Event()
    threading.Timer(0.05, cancelled.set).start()
    started = time.perf_counter()
    outputs = _executor(handler).run_many([_call("web_search", query="slow")], is_cancelled=cancelled.is_set)
    release.set()
    assert outputs == [None]
    assert time.perf_counter() - started < 2.0


def test_memory_profile_survives_an_unreachable_sidecar():
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    assert _executor(handler).memory_profile() == ""
