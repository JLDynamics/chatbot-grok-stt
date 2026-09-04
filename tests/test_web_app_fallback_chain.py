"""Signals the content fallback chain needs: why a rung failed, and where to go next.

A failed read is only useful if the caller can tell *why* it failed and, when a
page is involved, *which* page. These cover the sidecar half of that contract.
"""

import importlib.util
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

WEB_APP_DIR = Path(__file__).resolve().parents[1] / "web_app"
spec = importlib.util.spec_from_file_location("chatbot_web_app_server_fallback", WEB_APP_DIR / "server.py")
assert spec and spec.loader
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)

client = TestClient(server.app)
BRIDGE_HEADERS = {"X-Chatbot-Bridge": "page-v1"}


@pytest.fixture(autouse=True)
def _clean_bridge_state():
    server.browser_pages.clear()
    server.last_seen_pages.clear()
    yield
    server.browser_pages.clear()
    server.last_seen_pages.clear()


def _publish(tab_id: str = "1", url: str = "https://example.com/story", title: str = "Story") -> None:
    # Mirrors what the extension posts: _validate_browser_page requires a
    # recognised source/content_type/boundary triple for a plain web page.
    body = {
        "tab_id": tab_id,
        "url": url,
        "title": title,
        "text": "word " * 200,
        "source": "browser_dom",
        "content_type": "article",
        "boundary": "semantic_article_end",
    }
    response = client.post("/api/browser/page", json=body, headers=BRIDGE_HEADERS)
    assert response.status_code == 200, response.text


# ── bridge failure carries a reason, and a URL when one is known ──────────


def test_bridge_read_reports_never_enabled_when_no_page_was_ever_shared():
    detail = client.post("/api/browser/read").json()["detail"]
    assert detail["reason"] == "bridge_never_enabled"
    assert "url" not in detail
    assert "toolbar icon" in detail["message"]


def test_bridge_read_still_names_the_page_after_its_text_expires(monkeypatch):
    _publish(url="https://example.com/expired-story", title="Expired Story")

    # Age past the text TTL but inside the address TTL.
    aged = server.time.monotonic() + server.BROWSER_PAGE_TTL_S + 1
    monkeypatch.setattr(server.time, "monotonic", lambda: aged)

    response = client.post("/api/browser/read")
    assert response.status_code == 503
    detail = response.json()["detail"]
    assert detail["reason"] == "bridge_expired"
    assert detail["url"] == "https://example.com/expired-story"
    assert detail["title"] == "Expired Story"


def test_address_is_forgotten_once_it_too_expires(monkeypatch):
    _publish()
    aged = server.time.monotonic() + server.BROWSER_URL_TTL_S + 1
    monkeypatch.setattr(server.time, "monotonic", lambda: aged)

    detail = client.post("/api/browser/read").json()["detail"]
    assert detail["reason"] == "bridge_never_enabled"


# ── the retained map holds an address and nothing more ────────────────────


def test_retained_entry_never_holds_page_text():
    _publish(url="https://example.com/secret", title="Secret")
    assert list(server.last_seen_pages.values()) == [
        (pytest.approx(server.last_seen_pages["1"][0]), "https://example.com/secret", "Secret")
    ]
    assert "word" not in repr(server.last_seen_pages)


def test_hiding_a_tab_forgets_its_address_too():
    _publish(tab_id="7")
    assert "7" in server.last_seen_pages

    response = client.post("/api/browser/hide", json={"tab_id": "7"}, headers=BRIDGE_HEADERS)
    assert response.status_code == 200
    assert "7" not in server.last_seen_pages
    assert client.post("/api/browser/read").json()["detail"]["reason"] == "bridge_never_enabled"


def test_clearing_the_session_forgets_every_address():
    _publish(tab_id="1")
    _publish(tab_id="2", url="https://example.com/other")

    assert client.post("/api/browser/clear", headers=BRIDGE_HEADERS).status_code == 200
    assert server.last_seen_pages == {}


def test_retained_addresses_stay_bounded():
    for tab in range(1, server.BROWSER_PAGE_MAX_ENTRIES + 12):
        _publish(tab_id=str(tab), url=f"https://example.com/{tab}")
    assert len(server.last_seen_pages) <= server.BROWSER_PAGE_MAX_ENTRIES


# ── gated pages are distinguishable from real ones ────────────────────────


@pytest.mark.parametrize("status", sorted(server.GATED_STATUS_CODES))
def test_refusal_status_codes_are_reported_as_gated(status):
    assert server._looks_gated(status, "Some page body") == f"http_{status}"


def test_short_interstitial_text_is_reported_as_gated():
    assert server._looks_gated(200, "Subscribe to continue reading this article.") == "paywall_or_interstitial"


def test_a_real_article_is_not_reported_as_gated():
    # Long body: the marker heuristic must not fire on an article that merely
    # mentions subscribing, or every fetch would escalate for nothing.
    article = "Subscribe to continue reading is a phrase this article discusses. " * 40
    assert server._looks_gated(200, article) is None
    assert server._looks_gated(200, "word " * 500) is None


def test_gating_needs_a_marker_not_merely_brevity():
    assert server._looks_gated(200, "Short but genuine answer.") is None


# ── the panel must not race its own response ──────────────────────────────

VOICE_SOURCES = Path(__file__).resolve().parents[1] / "macos" / "Voice" / "Sources" / "Session"


def test_tool_result_defers_response_create_until_the_active_one_finishes():
    """A fast tool must not request a response while one is still streaming.

    The server rejects an overlapping create with
    ``conversation_already_has_active_response`` and nothing retried it, so the
    turn ended after the spoken acknowledgement and the tool result was never
    used. That also caps any fallback chain at its first rung, since every rung
    past the first needs another response.
    """
    live = (VOICE_SOURCES / "LiveVoiceBackend.swift").read_text()
    assert "private var responseRequestPending = false" in live
    # requestResponse defers rather than sending straight out...
    assert "guard activeResponseId.isEmpty else {" in live
    assert "responseRequestPending = true" in live
    # ...and response.done flushes whatever was deferred.
    assert "if responseRequestPending, activeResponseId.isEmpty {" in live
    assert "sendResponseCreate()" in live
    # Per-session teardown must not leak a pending request into the next session.
    assert 'activeResponseId = ""\n        responseRequestPending = false' in live


def test_every_tool_has_a_progress_label():
    """Each tool the executor dispatches shows something while it runs."""
    session = (VOICE_SOURCES / "VoiceSession.swift").read_text()
    tools = (VOICE_SOURCES / "VoiceTools.swift").read_text()
    dispatched = set(re.findall(r'^\s*case "([a-z_]+)":$', tools, re.M))
    labelled = set(re.findall(r'case "([a-z_]+)": desc =', session))
    assert dispatched, "no tool cases found; the dispatch shape changed"
    assert dispatched <= labelled, f"tools with no progress label: {sorted(dispatched - labelled)}"


# ── scrolling must actually reach the app it names ────────────────────────


def _allow_desktop(monkeypatch, tmp_path):
    harness = tmp_path / "desktop-harness"
    harness.write_text("#!/bin/sh\n")
    harness.chmod(0o700)
    monkeypatch.setattr(server, "DESKTOP_HARNESS_BIN", harness)
    monkeypatch.setattr(server, "DESKTOP_CONTROL_ENABLED", True)

    async def safe_scope(_app=None):
        return None

    monkeypatch.setattr(server, "_screen_scope_looks_sensitive", safe_scope)


def test_scroll_focuses_and_points_at_the_named_app(monkeypatch, tmp_path):
    """macOS sends wheel events to the focused app, at the pointer.

    Without both, scroll() silently does nothing while still reporting ok, so a
    caller reading a long page loops forever on one screenful — the screenshots
    come back byte-identical every round.
    """
    _allow_desktop(monkeypatch, tmp_path)
    seen = {}

    async def fake_harness(script, _timeout):
        seen["script"] = script
        return 0, '{"ok": true}'

    monkeypatch.setattr(server, "_run_harness", fake_harness)
    response = client.post("/api/desktop/act", json={"action": "scroll", "app": "Google Chrome", "amount": 8})
    assert response.status_code == 200

    script = seen["script"]
    assert "open_app(app)" in script, "scroll must focus the target app"
    assert "window_frame(app)" in script and "move_to(" in script, "pointer must land inside the window"
    # Ordering matters: focusing after the wheel event is useless.
    assert script.index("open_app(app)") < script.index("scroll(dy=")
    assert script.index("move_to(") < script.index("scroll(dy=")


def test_scroll_without_a_target_does_not_steal_focus(monkeypatch, tmp_path):
    """No app named means no window to aim at, so don't yank focus around."""
    _allow_desktop(monkeypatch, tmp_path)
    seen = {}

    async def fake_harness(script, _timeout):
        seen["script"] = script
        return 0, '{"ok": true}'

    monkeypatch.setattr(server, "_run_harness", fake_harness)
    assert client.post("/api/desktop/act", json={"action": "scroll", "amount": 3}).status_code == 200
    assert "open_app(app)" not in seen["script"]


def test_debounced_save_detaches_before_flushing():
    """The debounced save must not cancel the task it is running inside.

    flushSave() cancels saveTask so a direct save supersedes a pending
    debounce. Reached from the debounce, though, saveTask *is* that task, so it
    cancelled itself and URLSession aborted the in-flight PATCH: every
    debounced save failed with "cancelled" and only session-switch or stop ever
    persisted the transcript.
    """
    session = (VOICE_SOURCES / "VoiceSession.swift").read_text()
    schedule = session[session.index("private func scheduleSave"):]
    schedule = schedule[: schedule.index("func flushSave")]
    assert "self?.saveTask = nil" in schedule, "debounce must detach before flushing"
    assert schedule.index("self?.saveTask = nil") < schedule.index("await self?.flushSave()")
