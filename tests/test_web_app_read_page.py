"""Tests for the sidecar-side `read_page` ladder.

The ladder used to live in Swift (`PageReadWorkflow`), which meant every rung
was a separate process hop from the LLM. These tests pin the server-side
behaviour: which rung runs first, how the fallbacks chain, and the shape of the
structured result the model reads.
"""

import importlib.util
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

WEB_APP_DIR = Path(__file__).resolve().parents[1] / "web_app"
spec = importlib.util.spec_from_file_location("chatbot_web_app_server_read_page", WEB_APP_DIR / "server.py")
assert spec and spec.loader
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)
client = TestClient(server.app)

BRIDGE = {"X-Chatbot-Bridge": "page-v1"}
ARTICLE_TEXT = "Readable main content sentence. " * 20


def _bridge_page(url: str = "https://example.com/article", title: str = "Example") -> dict:
    return {
        "url": url,
        "title": title,
        "text": ARTICLE_TEXT,
        "source": "browser_dom",
        "content_type": "article",
        "complete": True,
        "boundary": "semantic_article_end",
    }


def _fetched(url: str, text: str = "Fetched readable text.", **extra) -> dict:
    return {"url": url, "title": "Fetched", "text": text, "truncated": False, "gated": False, **extra}


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    monkeypatch.setattr(server, "_is_public_url", lambda _url: (True, ""))
    server.browser_pages.clear()
    server.last_seen_pages.clear()
    yield
    server.browser_pages.clear()
    server.last_seen_pages.clear()


def _fake_fetch(result, calls: list[str] | None = None):
    async def fetch(url: str):
        if calls is not None:
            calls.append(url)
        if isinstance(result, Exception):
            raise result
        return dict(result)

    return fetch


def _methods(body: dict) -> list[str]:
    return [attempt["method"] for attempt in body["attempts"]]


def test_bridge_runs_first_when_asked_and_fetch_is_skipped(monkeypatch):
    assert client.post("/api/browser/page", headers=BRIDGE, json=_bridge_page()).status_code == 200
    monkeypatch.setattr(server, "_fetch", _fake_fetch(AssertionError("fetch must not run")))

    body = client.post("/api/read_page", json={"prefer_browser": True}).json()
    assert body["status"] == "read"
    assert body["source"] == "chrome_bridge"
    assert body["title"] == "Example"
    assert body["text"].startswith("Readable main content")
    assert body["complete"] is True
    assert body["attempts"] == [{"method": "chrome_bridge", "status": "read", "reason": ""}]


def test_no_url_and_empty_bridge_explains_how_to_share(monkeypatch):
    monkeypatch.setattr(server, "_fetch", _fake_fetch(AssertionError("no url, so no fetch")))
    body = client.post("/api/read_page", json={}).json()
    assert body["status"] == "unavailable"
    assert body["complete"] is False
    assert body["attempts"] == [{"method": "chrome_bridge", "status": "failed", "reason": "bridge_never_enabled"}]
    assert "toolbar icon" in body["message"]
    assert "url" not in body


def test_expired_bridge_still_names_the_page_so_fetch_can_run(monkeypatch):
    server.last_seen_pages["7"] = (server.time.monotonic(), "https://example.com/seen", "Seen")
    calls: list[str] = []
    monkeypatch.setattr(server, "_fetch", _fake_fetch(_fetched("https://example.com/seen"), calls))

    body = client.post("/api/read_page", json={}).json()
    assert calls == ["https://example.com/seen"]
    assert body["status"] == "read"
    assert body["source"] == "web_fetch"
    assert _methods(body) == ["chrome_bridge", "web_fetch"]
    assert body["attempts"][0]["reason"] == "bridge_expired"


def test_url_prefers_fetch_and_falls_back_to_bridge_on_error(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        server,
        "_fetch",
        _fake_fetch(server.HTTPException(status_code=502, detail="Could not fetch that page."), calls),
    )
    assert client.post("/api/browser/page", headers=BRIDGE, json=_bridge_page()).status_code == 200

    body = client.post("/api/read_page", json={"url": "https://example.com/article"}).json()
    assert calls == ["https://example.com/article"]
    assert body["status"] == "read"
    assert body["source"] == "chrome_bridge"
    assert _methods(body) == ["web_fetch", "chrome_bridge"]
    assert body["attempts"][0] == {"method": "web_fetch", "status": "failed", "reason": "Could not fetch that page."}


def test_successful_fetch_returns_without_touching_the_bridge(monkeypatch):
    monkeypatch.setattr(server, "_fetch", _fake_fetch(_fetched("https://example.com/article")))
    assert client.post("/api/browser/page", headers=BRIDGE, json=_bridge_page()).status_code == 200

    body = client.post("/api/read_page", json={"url": "example.com/article"}).json()
    assert body["status"] == "read"
    assert body["source"] == "web_fetch"
    assert body["text"] == "Fetched readable text."
    assert body["attempts"] == [{"method": "web_fetch", "status": "read", "reason": ""}]


def test_bridge_never_substitutes_a_different_open_page(monkeypatch):
    other = _bridge_page("https://other.example/post", "Other")
    assert client.post("/api/browser/page", headers=BRIDGE, json=other).status_code == 200
    monkeypatch.setattr(server, "_fetch", _fake_fetch(_fetched("https://example.com/article")))

    body = client.post("/api/read_page", json={"url": "https://example.com/article", "prefer_browser": True}).json()
    assert body["status"] == "read"
    assert body["source"] == "web_fetch"
    assert body["attempts"][0] == {"method": "chrome_bridge", "status": "wrong_page", "reason": "wrong_page"}


def test_gated_fetch_is_reported_as_blocked_and_bridge_is_tried(monkeypatch):
    gated = _fetched(
        "https://example.com/paywalled",
        "Subscribe to read the rest of this article.",
        gated=True,
        gated_reason="paywall_or_interstitial",
    )
    monkeypatch.setattr(server, "_fetch", _fake_fetch(gated))

    body = client.post("/api/read_page", json={"url": "https://example.com/paywalled"}).json()
    assert body["status"] == "unavailable"
    assert body["url"] == "https://example.com/paywalled"
    assert body["attempts"] == [
        {"method": "web_fetch", "status": "blocked", "reason": "paywall_or_interstitial"},
        {"method": "chrome_bridge", "status": "failed", "reason": "bridge_never_enabled"},
    ]
    assert "Chrome" in body["message"]
    json.dumps(body)


def test_truncated_fetch_is_kept_as_partial_unless_bridge_completes_it(monkeypatch):
    truncated = _fetched("https://example.com/long", "Half the article...", truncated=True)
    monkeypatch.setattr(server, "_fetch", _fake_fetch(truncated))

    body = client.post("/api/read_page", json={"url": "https://example.com/long"}).json()
    assert body["status"] == "partial"
    assert body["source"] == "web_fetch"
    assert body["complete"] is False
    assert _methods(body) == ["web_fetch", "chrome_bridge"]

    assert (
        client.post("/api/browser/page", headers=BRIDGE, json=_bridge_page("https://example.com/long")).status_code
        == 200
    )
    body = client.post("/api/read_page", json={"url": "https://example.com/long"}).json()
    assert body["status"] == "read"
    assert body["source"] == "chrome_bridge"
    assert body["complete"] is True
    assert [attempt["status"] for attempt in body["attempts"]] == ["partial", "read"]


def test_x_links_go_to_the_bridge_first(monkeypatch):
    monkeypatch.setattr(server, "_fetch", _fake_fetch(AssertionError("x.com must not be fetched first")))
    x_post = {
        "url": "https://x.com/someone/status/12345",
        "title": "Post",
        "text": "Short post.",
        "source": "x_post_dom",
        "content_type": "x_post",
        "complete": True,
        "comments_excluded": True,
        "boundary": "x_primary_post_end",
    }
    assert client.post("/api/browser/page", headers=BRIDGE, json=x_post).status_code == 200

    body = client.post("/api/read_page", json={"url": "https://twitter.com/someone/status/12345"}).json()
    assert body["status"] == "read"
    assert body["source"] == "chrome_bridge"
    assert body["text"] == "Short post."
    assert _methods(body) == ["chrome_bridge"]
