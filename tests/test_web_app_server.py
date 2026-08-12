import importlib.util
import json
import os
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

WEB_APP_DIR = Path(__file__).resolve().parents[1] / "web_app"
ROOT = WEB_APP_DIR.parent
spec = importlib.util.spec_from_file_location("chatbot_web_app_server", WEB_APP_DIR / "server.py")
assert spec and spec.loader
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)
client = TestClient(server.app)


def test_config_exposes_retained_browser_capabilities(monkeypatch, tmp_path):
    harness = tmp_path / "desktop-harness"
    harness.write_text("#!/bin/sh\n")
    harness.chmod(0o700)
    monkeypatch.setattr(server, "DESKTOP_HARNESS_BIN", harness)
    monkeypatch.setattr(server, "DESKTOP_CONTROL_ENABLED", True)
    data = client.get("/api/config").json()
    assert data["s2sUrl"].endswith("/v1/realtime")
    assert data["allowDirect"] is False
    assert data["desktopControl"] is True

    monkeypatch.setattr(server, "DESKTOP_CONTROL_ENABLED", False)
    assert client.get("/api/config").json()["desktopControl"] is False

    monkeypatch.setattr(server, "DESKTOP_CONTROL_ENABLED", True)
    harness.unlink()
    assert client.get("/api/config").json()["desktopControl"] is False


def _enable_desktop_control(monkeypatch, tmp_path):
    harness = tmp_path / "desktop-harness"
    harness.write_text("#!/bin/sh\n")
    harness.chmod(0o700)
    monkeypatch.setattr(server, "DESKTOP_HARNESS_BIN", harness)
    monkeypatch.setattr(server, "DESKTOP_CONTROL_ENABLED", True)


def test_desktop_screenshot_returns_bounded_png_for_visible_target(monkeypatch, tmp_path):
    _enable_desktop_control(monkeypatch, tmp_path)
    capture_dir = tmp_path / "captures"
    capture_dir.mkdir()
    capture = capture_dir / "capture.png"
    capture.write_bytes(b"\x89PNG\r\n\x1a\n" + os.urandom(1_200))
    monkeypatch.setattr(server, "DESKTOP_CAPTURE_DIR", capture_dir)

    async def safe_scope(_app=None):
        return None

    async def fake_harness(script, _timeout):
        assert "screenshot(app='Safari')" in script
        return 0, json.dumps({"path": str(capture)})

    monkeypatch.setattr(server, "_screen_scope_looks_sensitive", safe_scope)
    monkeypatch.setattr(server, "_run_harness", fake_harness)
    response = client.post("/api/desktop/act", json={"action": "screenshot", "app": "Safari"})
    assert response.status_code == 200
    body = response.json()
    assert body["action"] == "screenshot"
    assert body["target"] == "Safari"
    assert body["path"] == str(capture)
    assert body["image"].startswith("data:image/png;base64,")


def test_desktop_screenshot_reports_permission_and_sensitive_scope(monkeypatch, tmp_path):
    _enable_desktop_control(monkeypatch, tmp_path)

    async def safe_scope(_app=None):
        return None

    async def denied_harness(_script, _timeout):
        return 1, "capture returned no image — grant Screen Recording"

    monkeypatch.setattr(server, "_screen_scope_looks_sensitive", safe_scope)
    monkeypatch.setattr(server, "_run_harness", denied_harness)
    response = client.post("/api/desktop/act", json={"action": "screenshot"})
    assert response.status_code == 403
    assert "Screen Recording" in response.json()["detail"]

    async def sensitive_scope(_app=None):
        return "payment"

    monkeypatch.setattr(server, "_screen_scope_looks_sensitive", sensitive_scope)
    response = client.post("/api/desktop/act", json={"action": "screenshot", "app": "Checkout"})
    assert response.status_code == 451


def test_fetch_rejects_local_addresses(monkeypatch):
    monkeypatch.setattr(server.socket, "getaddrinfo", lambda *_: [(None, None, None, None, ("127.0.0.1", 0))])
    response = client.post("/api/fetch", json={"url": "http://example.test/private"})
    assert response.status_code == 400
    assert "private or loopback" in response.json()["detail"]


@pytest.mark.asyncio
async def test_fetch_extracts_main_text_and_reports_truncation(monkeypatch):
    article = " ".join(["Natural chatbot comparison text."] * 30)
    response = httpx.Response(
        200,
        headers={"content-type": "text/html; charset=utf-8"},
        content=f"<html><head><title>Example</title></head><body><nav>Noise</nav><main>{article}</main></body></html>".encode(),
        request=httpx.Request("GET", "https://example.com/article"),
    )

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def get(self, _url):
            return response

    monkeypatch.setattr(server, "_is_public_url", lambda _url: (True, ""))
    monkeypatch.setattr(server.httpx, "AsyncClient", FakeClient)
    result = await server.fetch_page(server.FetchRequest(url="https://example.com/article"))
    body = json.loads(result.body)
    assert body["title"] == "Example"
    assert body["text"].startswith("Natural chatbot comparison text.")
    assert "Noise" not in body["text"]
    assert body["truncated"] is False


def test_chrome_bridge_requires_header_and_returns_fresh_page(monkeypatch):
    monkeypatch.setattr(server, "_is_public_url", lambda _url: (True, ""))
    server.browser_pages.clear()
    payload = {
        "url": "https://example.com/article",
        "title": "Example",
        "text": "Readable main content. " * 20,
        "source": "browser_dom",
        "content_type": "article",
        "complete": True,
        "boundary": "semantic_article_end",
    }
    assert client.post("/api/browser/page", json=payload).status_code == 403
    stored = client.post("/api/browser/page", headers={"X-Chatbot-Bridge": "page-v1"}, json=payload)
    assert stored.status_code == 200
    page = client.post("/api/browser/read").json()
    assert page["source"] == "browser_dom"
    assert page["title"] == "Example"


def test_chrome_bridge_hides_tab_scoped_cache(monkeypatch):
    monkeypatch.setattr(server, "_is_public_url", lambda _url: (True, ""))
    server.browser_pages.clear()
    payload = {
        "tab_id": "321",
        "url": "https://example.com/article",
        "title": "Visible article",
        "text": "Readable main content. " * 20,
        "source": "browser_dom",
        "content_type": "article",
        "complete": True,
        "boundary": "semantic_article_end",
    }
    headers = {"X-Chatbot-Bridge": "page-v1"}
    assert client.post("/api/browser/page", headers=headers, json=payload).status_code == 200
    assert client.post("/api/browser/read").json()["title"] == "Visible article"

    assert client.post("/api/browser/hide", json={"tab_id": "321"}).status_code == 403
    hidden = client.post("/api/browser/hide", headers=headers, json={"tab_id": "321"})
    assert hidden.json() == {"ok": True, "removed": True}
    assert client.post("/api/browser/read").status_code == 503


def test_chrome_bridge_clear_ends_session_and_discards_all_cached_pages(monkeypatch):
    monkeypatch.setattr(server, "_is_public_url", lambda _url: (True, ""))
    server.browser_pages.clear()
    headers = {"X-Chatbot-Bridge": "page-v1"}
    for tab_id in ("321", "654"):
        payload = {
            "tab_id": tab_id,
            "url": f"https://example.com/article/{tab_id}",
            "title": f"Visible article {tab_id}",
            "text": "Readable main content. " * 20,
            "source": "browser_dom",
            "content_type": "article",
            "complete": True,
            "boundary": "semantic_article_end",
        }
        assert client.post("/api/browser/page", headers=headers, json=payload).status_code == 200

    assert client.post("/api/browser/clear").status_code == 403
    cleared = client.post("/api/browser/clear", headers=headers)
    assert cleared.json() == {"ok": True, "removed": 2}
    assert client.post("/api/browser/read").status_code == 503


def test_chrome_bridge_republishes_visible_tab_and_routes_article_text_without_screenshots():
    content = (WEB_APP_DIR / "chrome_article_bridge" / "content.js").read_text()
    background = (WEB_APP_DIR / "chrome_article_bridge" / "background.js").read_text()
    main = (WEB_APP_DIR / "main.js").read_text()
    manifest = json.loads((WEB_APP_DIR / "chrome_article_bridge" / "manifest.json").read_text())

    assert "document.addEventListener('visibilitychange', handleVisibilityChange)" in content
    assert "publish(true);" in content
    assert "{ type: 'hide-page' }" in content
    assert "chrome.windows.getLastFocused" in background
    assert "sender.tab?.active && sender.tab.windowId === focusedWindow.id" in background
    assert "if (!await senderIsCurrent(sender))" in background
    assert "tab_id: String(tabId)" in background
    assert "chatbotReceiverIsActive" in background
    assert "chrome.storage.session" in background
    assert "chatbotPageBridgeSession" in background
    assert "chrome.tabs.onRemoved" in background
    assert "chrome.tabs.onActivated" in background
    assert "chrome.alarms.onAlarm" in background
    assert "periodInMinutes: 0.5" in background
    assert "CLEAR_ENDPOINT" in background
    assert manifest["permissions"] == ["storage", "alarms"]
    assert manifest["version"] == "0.4.1"
    assert "sendResponse({ ok: true, enabled: true, preserved: true })" in background
    assert "function extractXPost()" in content
    assert "x_primary_post_end" in content
    assert '[data-testid="tweetText"]' in content
    assert "function runtimeMessage(message" in content
    assert "status delivery failed" in content
    assert "function markContextInvalidated(error)" in content
    assert "observer?.disconnect()" in content
    assert "Reload this page once to activate the new content script" in content
    assert "sendResponse({ ok: true })" in content
    routing_examples = (
        "requires no approval or confirmation",
        "text request, call read_article directly",
        "screen'. Use inspect_current_context only when the request is genuinely ambiguous",
        "read_article means call read_article",
        "control_screen_screenshot means call control_screen",
        "Article, news, webpage, page, or individual X post requests",
        "check, read, grab, summarize, analyze, or",
        "screen/app/window, layout, image, chart, visual appearance, or front-page requests",
        "An explicit 'take a screenshot'",
    )
    for example in routing_examples:
        assert example in main
    assert "no page body and no screenshot" in main
    assert "do not ask for approval and do not offer or call" in main
    assert "content-versus-visual question" in main
    assert "After preflight, call the chosen content tool immediately without another spoken update" in main
    assert "Do not use read_article for generic visual " in main
    assert "screen requests." in main
    assert 'name: "inspect_current_context"' in main
    assert "Do not call it for an explicit " in main
    assert "X-post text request; call read_article directly" in main
    assert 'fetch("api/context/preflight"' in main
    assert "base + TOOL_USE_HINT + TOOL_INTENT_ROUTING" in main


def test_chrome_bridge_session_persists_until_disabled_or_receiver_closes():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required")
    script = r"""
import { readFileSync } from "node:fs";

let listener;
let clickListener;
let removeListener;
let activateListener;
let alarmListener;
let updateListener;
let activeReceiver = false;
let receiverExists = false;
let serverAvailable = true;
let stored = {};
let badgeText = null;
let pageCount = 0;
let hideCount = 0;
let clearCount = 0;
globalThis.chrome = {
  action: {
    setBadgeText: async ({ text }) => { badgeText = text; },
    setBadgeBackgroundColor: async () => {},
    setTitle: async () => {},
    onClicked: { addListener(fn) { clickListener = fn; } },
  },
  runtime: {
    lastError: null,
    onMessage: { addListener(fn) { listener = fn; } },
  },
  alarms: {
    create: async () => {},
    clear: async () => true,
    onAlarm: { addListener(fn) { alarmListener = fn; } },
  },
  storage: {
    session: {
      get: async (key) => ({ [key]: stored[key] }),
      set: async (value) => { stored = { ...stored, ...value }; },
      remove: async (key) => { delete stored[key]; },
    },
  },
  tabs: {
    sendMessage(_tabId, _message, callback) { callback?.(); },
    query: async (query) => query.lastFocusedWindow
      ? (activeReceiver ? [{ id: 99, url: "http://127.0.0.1:7860/" }] : [])
      : (receiverExists ? [{ id: 99, url: "http://127.0.0.1:7860/" }] : []),
    onRemoved: { addListener(fn) { removeListener = fn; } },
    onActivated: { addListener(fn) { activateListener = fn; } },
    onUpdated: { addListener(fn) { updateListener = fn; } },
  },
  windows: { getLastFocused: async () => ({ id: 3 }) },
};
globalThis.fetch = async (url) => {
  if (url.endsWith("/page")) pageCount += 1;
  if (url.endsWith("/hide")) hideCount += 1;
  if (url.endsWith("/clear")) clearCount += 1;
  if (url.endsWith("/status") && !serverAvailable) {
    return {
      ok: false,
      status: 503,
      text: async () => "unavailable",
      json: async () => ({ expected_version: "0.4.1" }),
    };
  }
  return {
    ok: true,
    text: async () => "",
    json: async () => ({ expected_version: "0.4.1" }),
  };
};
eval(readFileSync("web_app/chrome_article_bridge/background.js", "utf8"));

const wait = () => new Promise((resolve) => setTimeout(resolve, 20));

async function call(message) {
  return await new Promise((resolve, reject) => {
    const keptOpen = listener(
      message,
      { tab: { id: 17, active: true, windowId: 3 } },
      resolve,
    );
    if (keptOpen !== true) reject(new Error("message port was not kept open"));
    setTimeout(() => reject(new Error("background did not acknowledge message")), 500);
  });
}

await wait();
const disabledPublish = await call({
  type: "publish-page",
  page: { text: "Readable page text" },
});
if (!disabledPublish.disabled || pageCount !== 0) {
  throw new Error("disabled bridge published page text");
}

receiverExists = true;
updateListener(99, { url: "http://127.0.0.1:7860/" });
await wait();
const receiverOpened = await call({ type: "bridge-status", state: "receiver" });
if (!receiverOpened.ok || !receiverOpened.enabled) {
  throw new Error("opening the receiver did not automatically enable the bridge");
}
if (!stored.chatbotPageBridgeSession?.enabled || badgeText !== "✓") {
  throw new Error("automatic activation did not persist session and green badge");
}

const published = await call({
  type: "publish-page",
  page: { text: "Readable page text" },
});
if (!published.ok || pageCount !== 1 || badgeText !== "✓") {
  throw new Error("enabled bridge did not publish page text");
}

const hidden = await call({ type: "hide-page" });
if (!hidden.ok || hideCount !== 1 || badgeText !== "✓") {
  throw new Error("tab switch did not retain activation and remove the old tab");
}
activateListener({ tabId: 18 });
await wait();
if (!stored.chatbotPageBridgeSession?.enabled || badgeText !== "✓") {
  throw new Error("tab activation lost the saved session or green badge");
}
activeReceiver = true;
const preserved = await call({ type: "hide-page" });
if (!preserved.ok || !preserved.preserved) throw new Error("chatbot handoff was not preserved");
if (hideCount !== 1 || badgeText !== "✓") {
  throw new Error("chatbot handoff incorrectly removed the page or green state");
}

const blocked = await call({ type: "bridge-status", state: "blocked" });
if (!blocked.ok || !blocked.enabled || clearCount !== 2 || badgeText !== "✓") {
  throw new Error("blocked page did not clear stale text while retaining activation");
}

clickListener({ id: 17, active: true, windowId: 3 });
await wait();
if (!stored.chatbotPageBridgeSession?.enabled || badgeText !== "✓") {
  throw new Error("toolbar retry unexpectedly disabled the automatic session");
}

serverAvailable = false;
alarmListener({ name: "chatbotPageBridgeHealth" });
await wait();
if (stored.chatbotPageBridgeSession || badgeText !== "!") {
  throw new Error("health alarm did not detect server stop and clear the session");
}

serverAvailable = true;
const restarted = await call({ type: "bridge-status", state: "receiver" });
if (!restarted.ok || !stored.chatbotPageBridgeSession?.enabled || badgeText !== "✓") {
  throw new Error("receiver heartbeat did not reactivate after server restart");
}
updateListener(99, { url: "https://example.com/left-chatbot" });
await wait();
if (stored.chatbotPageBridgeSession || badgeText !== "") {
  throw new Error("navigating the receiver away did not end the session");
}
updateListener(99, { url: "http://127.0.0.1:7860/" });
await wait();
if (!stored.chatbotPageBridgeSession?.enabled || badgeText !== "✓") {
  throw new Error("returning the receiver did not automatically reactivate the session");
}
removeListener(99);
await wait();
if (stored.chatbotPageBridgeSession || badgeText !== "") {
  throw new Error("closing the Chatbot tab did not end the bridge session");
}
"""
    subprocess.run(
        [node, "--input-type=module", "-e", script],
        cwd=ROOT,
        check=True,
        timeout=5,
    )


def test_expected_websocket_response_race_is_not_logged_as_server_error():
    source = (WEB_APP_DIR / "ws" / "s2s-ws-client.js").read_text()
    handled = source.index('err?.type === "conversation_already_has_active_response"')
    recovered = source.index("response-create race recovered", handled)
    unexpected_log = source.index("[ws] server error (", recovered)
    assert handled < recovered < unexpected_log


def test_context_preflight_routes_without_page_text_or_screenshot(monkeypatch):
    monkeypatch.setattr(server, "_is_public_url", lambda _url: (True, ""))
    monkeypatch.setattr(server, "_desktop_control_available", lambda: True)
    server.browser_pages.clear()
    payload = {
        "tab_id": "456",
        "url": "https://example.com/news",
        "title": "Example News",
        "text": "Private body must not appear in preflight. " * 20,
        "source": "browser_dom",
        "content_type": "article",
        "complete": True,
        "boundary": "semantic_article_end",
    }
    headers = {"X-Chatbot-Bridge": "page-v1"}
    assert client.post("/api/browser/page", headers=headers, json=payload).status_code == 200

    async def chrome_context():
        return {
            "available": True,
            "sensitive": False,
            "app": "Google Chrome",
            "window_title": "Example News",
        }

    monkeypatch.setattr(server, "_desktop_frontmost_context", chrome_context)
    chrome = client.post("/api/context/preflight", json={"include_desktop": True}).json()
    assert chrome["route_hint"] == "read_article"
    assert chrome["chrome_bridge"]["fresh_readable_page"] is True
    assert chrome["desktop"]["app"] == "Google Chrome"
    assert chrome["contains_page_text"] is False
    assert chrome["captured_screenshot"] is False
    assert chrome["authorization"] == {
        "public_page_text": "no_confirmation_required",
        "desktop_visual_or_action": "explicit_user_request_required",
    }
    assert "Private body" not in json.dumps(chrome)

    main = (WEB_APP_DIR / "main.js").read_text()
    assert "no additional approval was required" in main
    assert "Do not ask the user for approval and do not fall back to a screenshot" in main

    async def notes_context():
        return {"available": True, "sensitive": False, "app": "Notes", "window_title": "Shopping list"}

    monkeypatch.setattr(server, "_desktop_frontmost_context", notes_context)
    other_app = client.post("/api/context/preflight", json={"include_desktop": True}).json()
    assert other_app["route_hint"] == "control_screen_screenshot"
    assert other_app["desktop"]["app"] == "Notes"

    server.browser_pages.clear()
    monkeypatch.setattr(server, "_desktop_frontmost_context", chrome_context)
    unsupported = client.post("/api/context/preflight", json={"include_desktop": True}).json()
    assert unsupported["route_hint"] == "ask"

    async def sensitive_context():
        return {"available": True, "sensitive": True, "app": "", "window_title": ""}

    monkeypatch.setattr(server, "_desktop_frontmost_context", sensitive_context)
    sensitive = client.post("/api/context/preflight", json={"include_desktop": True}).json()
    assert sensitive["route_hint"] == "ask"


def test_chrome_bridge_validates_generic_and_x_page_boundaries(monkeypatch):
    monkeypatch.setattr(server, "_is_public_url", lambda _url: (True, ""))
    server.browser_pages.clear()
    generic = {
        "url": "https://example.com/article",
        "title": "Example",
        "text": "Readable main content. " * 20,
        "source": "browser_dom",
        "content_type": "article",
        "complete": True,
        "boundary": "semantic_article_end",
    }
    invalid_generic = {**generic, "content_type": "form"}
    assert (
        client.post(
            "/api/browser/page",
            headers={"X-Chatbot-Bridge": "page-v1"},
            json=invalid_generic,
        ).status_code
        == 400
    )

    x_article = {
        "url": "https://x.com/example/article/123456789",
        "article_id": "123456789",
        "title": "Example X Article",
        "text": "Complete long-form article paragraph. " * 25,
        "source": "x_dom",
        "content_type": "x_article",
        "complete": True,
        "comments_excluded": True,
        "boundary": "x_article_body_end",
    }
    stored = client.post(
        "/api/browser/page",
        headers={"X-Chatbot-Bridge": "page-v1"},
        json=x_article,
    )
    assert stored.status_code == 200
    page = client.post("/api/browser/read").json()
    assert page["source"] == "x_dom"
    assert page["comments_excluded"] is True

    x_post = {
        "bridge_version": "0.4.1",
        "tab_id": "654",
        "url": "https://x.com/example/status/987654321",
        "article_id": "987654321",
        "title": "X post by Example @example",
        "text": "One primary public X post.",
        "source": "x_post_dom",
        "content_type": "x_post",
        "complete": True,
        "comments_excluded": True,
        "replies_detected": True,
        "boundary": "x_primary_post_end",
    }
    stored_post = client.post("/api/browser/page", headers={"X-Chatbot-Bridge": "page-v1"}, json=x_post)
    assert stored_post.status_code == 200
    assert stored_post.json()["bridge_version"] == "0.4.1"
    post = client.post("/api/browser/read").json()
    assert post["content_type"] == "x_post"
    assert post["text"] == "One primary public X post."
    assert post["comments_excluded"] is True
    status = client.get("/api/browser/status").json()
    assert status["connected"] is True
    assert status["expected_version"] == "0.4.1"
    assert status["bridge_version"] == "0.4.1"
    assert status["content_type"] == "x_post"

    invalid_post = {**x_post, "comments_excluded": False}
    assert (
        client.post(
            "/api/browser/page",
            headers={"X-Chatbot-Bridge": "page-v1"},
            json=invalid_post,
        ).status_code
        == 400
    )

    invalid_x = {**x_article, "comments_excluded": False}
    assert (
        client.post(
            "/api/browser/page",
            headers={"X-Chatbot-Bridge": "page-v1"},
            json=invalid_x,
        ).status_code
        == 400
    )


def test_chrome_bridge_rejects_private_network_pages():
    server.browser_pages.clear()
    payload = {
        "url": "http://127.0.0.2/internal",
        "title": "Internal",
        "text": "Private page content. " * 20,
        "source": "browser_dom",
        "content_type": "main",
        "complete": True,
        "boundary": "main_content_end",
    }
    response = client.post(
        "/api/browser/page",
        headers={"X-Chatbot-Bridge": "page-v1"},
        json=payload,
    )
    assert response.status_code == 400
    assert "private or loopback" in response.json()["detail"]
