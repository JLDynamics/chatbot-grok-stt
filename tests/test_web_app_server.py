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


def test_config_exposes_retained_sidecar_capabilities(monkeypatch, tmp_path):
    harness = tmp_path / "desktop-harness"
    harness.write_text("#!/bin/sh\n")
    harness.chmod(0o700)
    monkeypatch.setattr(server, "DESKTOP_HARNESS_BIN", harness)
    monkeypatch.setattr(server, "DESKTOP_CONTROL_ENABLED", True)
    data = client.get("/api/config").json()
    assert data["chatbotUrl"].endswith("/v1/realtime")
    assert data["allowDirect"] is False
    assert data["desktopControl"] is True
    assert data["codeAgent"] is True
    assert data["webPort"] == server.WEB_PORT

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


def test_desktop_screenshot_ignores_sign_in_labels_on_normal_apps(monkeypatch, tmp_path):
    _enable_desktop_control(monkeypatch, tmp_path)
    capture_dir = tmp_path / "Library/Caches/desktop-harness/captures"
    capture_dir.mkdir(parents=True)
    capture = capture_dir / "capture.png"
    capture.write_bytes(b"\x89PNG\r\n\x1a\n" + os.urandom(1_200))
    monkeypatch.setattr(server, "DESKTOP_CAPTURE_DIR", capture_dir)

    async def login_labels(_app=None):
        return "sign in"

    async def fake_harness(script, _timeout):
        return 0, json.dumps({"path": str(capture), "bright_frac": 0.4, "samples": 64})

    monkeypatch.setattr(server, "_screen_scope_looks_sensitive", login_labels)
    monkeypatch.setattr(server, "_run_harness", fake_harness)
    response = client.post("/api/desktop/act", json={"action": "screenshot", "app": "Safari"})
    assert response.status_code == 200
    assert response.json()["image"].startswith("data:image/png;base64,")


@pytest.mark.asyncio
async def test_denied_screen_permission_never_invokes_capture(monkeypatch):
    import sys
    import types

    called = []
    quartz = types.ModuleType("Quartz")
    quartz.CGPreflightScreenCaptureAccess = lambda: False
    helpers = types.ModuleType("desktop_harness.helpers")
    helpers.screenshot = lambda **kwargs: called.append(kwargs)
    monkeypatch.setitem(sys.modules, "Quartz", quartz)
    monkeypatch.setitem(sys.modules, "desktop_harness.helpers", helpers)

    async def execute_script(script, _timeout):
        try:
            exec(script, {})
        except PermissionError as exc:
            return 1, str(exc)
        raise AssertionError("Denied permission must stop the capture script")

    monkeypatch.setattr(server, "_run_harness", execute_script)
    for _ in range(2):
        with pytest.raises(server.HTTPException) as error:
            await server._capture_desktop_screenshot(None)
        assert error.value.status_code == 403
        assert "Do not retry" in error.value.detail
    assert called == [], "Repeated requests must never reach the prompting capture API"


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

        async def get(self, _url, **_kwargs):
            return response

    monkeypatch.setattr(server, "_is_public_url", lambda _url: (True, ""))
    monkeypatch.setattr(server, "_client", lambda: FakeClient())
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
    assert "reinjectContentScript" in background
    assert "chrome.scripting?.executeScript" in background
    # activeTab is what makes reinjectContentScript actually work: executeScript
    # on an article tab needs it (or a host permission for every site), and
    # without it the toolbar click could not revive a tab orphaned by an
    # extension reload -- the click appeared to do nothing at all.
    assert manifest["permissions"] == ["storage", "alarms", "scripting", "activeTab"]
    assert "http://127.0.0.1:7860/*" in manifest["host_permissions"]
    assert manifest["version"] == "0.4.3"
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
      json: async () => ({ expected_version: "0.4.3" }),
    };
  }
  return {
    ok: true,
    text: async () => "",
    json: async () => ({ expected_version: "0.4.3" }),
  };
};
eval(readFileSync("web_app/chrome_article_bridge/background.js", "utf8"));

const wait = () => new Promise((resolve) => setTimeout(resolve, 20));

async function call(message, tab = { id: 17, active: true, windowId: 3 }) {
  return await new Promise((resolve, reject) => {
    const keptOpen = listener(
      message,
      { tab },
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

// Native-app mode: no receiver tab exists, so the toolbar click enables the
// same session without one. Publishing, the health alarm (server still
// enforced), and server-stop detection must all keep working.
receiverExists = false;
activeReceiver = false;
clickListener({ id: 17, active: true, windowId: 3 });
await wait();
if (!stored.chatbotPageBridgeSession?.enabled || badgeText !== "✓") {
  throw new Error("toolbar click did not enable native mode without a receiver tab");
}
if (stored.chatbotPageBridgeSession.native !== true) {
  throw new Error("native-mode session was not flagged as native");
}
const nativePublished = await call({
  type: "publish-page",
  page: { text: "Native mode page text" },
});
if (!nativePublished.ok || badgeText !== "✓") {
  throw new Error("native-mode session did not publish page text");
}
const beforeNativeHide = hideCount;
const nativeHandoff = await call({ type: "hide-page" });
if (!nativeHandoff.preserved || hideCount !== beforeNativeHide) {
  throw new Error("switching to the native app removed the selected Chrome article");
}
const otherTab = await call({ type: "hide-page" }, { id: 17, active: false, windowId: 3 });
if (otherTab.preserved || hideCount !== beforeNativeHide + 1) {
  throw new Error("switching Chrome tabs retained the old article");
}
// Re-evaluate the actual worker source with session storage intact, as MV3
// does after suspending an idle worker. Native mode must survive that restart.
eval(readFileSync("web_app/chrome_article_bridge/background.js", "utf8"));
await wait();
if (!stored.chatbotPageBridgeSession?.native || badgeText !== "✓") {
  throw new Error("worker restart incorrectly ended the native session");
}
alarmListener({ name: "chatbotPageBridgeHealth" });
await wait();
if (!stored.chatbotPageBridgeSession?.enabled || badgeText !== "✓") {
  throw new Error("health alarm wrongly ended native mode with no receiver tab");
}
serverAvailable = false;
alarmListener({ name: "chatbotPageBridgeHealth" });
await wait();
if (stored.chatbotPageBridgeSession || badgeText !== "!") {
  throw new Error("health alarm did not detect server stop in native mode");
}
"""
    subprocess.run(
        [node, "--input-type=module", "-e", script],
        cwd=ROOT,
        check=True,
        timeout=5,
    )


def test_bridge_toolbar_click_reinjects_stale_content_script():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required")
    script = r"""
import { readFileSync } from "node:fs";

let clickListener;
let stored = {};
const injected = [];
globalThis.chrome = {
  action: {
    setBadgeText: async () => {},
    setBadgeBackgroundColor: async () => {},
    setTitle: async () => {},
    onClicked: { addListener(fn) { clickListener = fn; } },
  },
  runtime: { lastError: null, onMessage: { addListener() {} } },
  scripting: {
    executeScript: async (details) => { injected.push(details); },
  },
  alarms: {
    create: async () => {},
    clear: async () => true,
    onAlarm: { addListener() {} },
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
    query: async () => [],
    onRemoved: { addListener() {} },
    onActivated: { addListener() {} },
    onUpdated: { addListener() {} },
  },
  windows: { getLastFocused: async () => ({ id: 3 }) },
};
globalThis.fetch = async () => ({
  ok: true,
  text: async () => "",
  json: async () => ({ expected_version: "0.4.3" }),
});
eval(readFileSync("web_app/chrome_article_bridge/background.js", "utf8"));

const wait = () => new Promise((resolve) => setTimeout(resolve, 20));
await wait();
clickListener({ id: 17, active: true, windowId: 3 });
await wait();
if (injected.length !== 1) {
  throw new Error("toolbar click did not re-inject the content script");
}
if (injected[0].target?.tabId !== 17 || !injected[0].files?.includes("content.js")) {
  throw new Error("re-inject targeted the wrong tab or file");
}
if (!stored.chatbotPageBridgeSession?.enabled) {
  throw new Error("toolbar click did not enable the session after re-inject");
}
"""
    subprocess.run(
        [node, "--input-type=module", "-e", script],
        cwd=ROOT,
        check=True,
        timeout=5,
    )


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
        "bridge_version": "0.4.3",
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
    assert stored_post.json()["bridge_version"] == "0.4.3"
    post = client.post("/api/browser/read").json()
    assert post["content_type"] == "x_post"
    assert post["text"] == "One primary public X post."
    assert post["comments_excluded"] is True
    status = client.get("/api/browser/status").json()
    assert status["connected"] is True
    assert status["expected_version"] == "0.4.3"
    assert status["bridge_version"] == "0.4.3"
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


@pytest.mark.asyncio
async def test_search_requires_key_and_returns_serper_results(monkeypatch):
    monkeypatch.setattr(server, "SERPER_KEY", "")
    monkeypatch.setattr(server, "TAVILY_KEY", "")
    monkeypatch.setattr(server, "TINYFISH_KEY", "")
    assert client.post("/api/search", json={"query": "chatbot"}).status_code == 503

    monkeypatch.setattr(server, "SERPER_KEY", "serper-test")
    response = httpx.Response(
        200,
        json={
            "organic": [
                {"title": "Example", "snippet": "A snippet", "link": "https://example.com"},
            ],
            "answerBox": {"answer": "42"},
        },
        request=httpx.Request("POST", server.SERPER_URL),
    )

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def post(self, url, headers=None, json=None, **_kwargs):
            assert url == server.SERPER_URL
            assert headers["X-API-KEY"] == "serper-test"
            assert json["q"] == "latest news"
            return response

    monkeypatch.setattr(server, "_client", lambda: FakeClient())
    body = client.post("/api/search", json={"query": "latest news"}).json()
    assert body["answer"] == "42"
    assert body["results"][0]["url"] == "https://example.com"


@pytest.mark.asyncio
async def test_search_prefers_tinyfish_when_configured(monkeypatch):
    monkeypatch.setattr(server, "TINYFISH_KEY", "sk-tinyfish-test")
    monkeypatch.setattr(server, "SERPER_KEY", "server-serper")
    monkeypatch.setattr(server, "TAVILY_KEY", "")
    response = httpx.Response(
        200,
        json={
            "query": "chatbot",
            "results": [
                {
                    "position": 1,
                    "site_name": "example.com",
                    "snippet": "Tiny result",
                    "title": "Example",
                    "url": "https://example.com",
                }
            ],
            "total_results": 1,
            "page": 0,
        },
        request=httpx.Request("GET", server.TINYFISH_SEARCH_URL),
    )

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def get(self, url, params=None, headers=None, **_kwargs):
            assert url == server.TINYFISH_SEARCH_URL
            assert headers["X-API-Key"] == "sk-tinyfish-test"
            assert params["query"] == "weather"
            return response

    monkeypatch.setattr(server, "_client", lambda: FakeClient())
    body = client.post("/api/search", json={"query": "weather"}).json()
    assert body["results"][0]["url"] == "https://example.com"
    assert body["answer"] is None


@pytest.mark.asyncio
async def test_fetch_uses_tinyfish_when_configured(monkeypatch):
    monkeypatch.setattr(server, "TINYFISH_KEY", "sk-tinyfish-test")
    monkeypatch.setattr(server, "_is_public_url", lambda _url: (True, ""))
    response = httpx.Response(
        200,
        json={
            "results": [
                {
                    "url": "https://example.com",
                    "final_url": "https://example.com",
                    "title": "Example Domain",
                    "text": "Example readable text.",
                    "format": "markdown",
                }
            ],
            "errors": [],
        },
        request=httpx.Request("POST", server.TINYFISH_FETCH_URL),
    )

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def post(self, url, headers=None, json=None, **_kwargs):
            assert url == server.TINYFISH_FETCH_URL
            assert headers["X-API-Key"] == "sk-tinyfish-test"
            assert json == {"urls": ["https://example.com"], "format": "markdown"}
            return response

    monkeypatch.setattr(server, "_client", lambda: FakeClient())
    body = client.post("/api/fetch", json={"url": "https://example.com"}).json()
    assert body["title"] == "Example Domain"
    assert body["text"] == "Example readable text."


@pytest.mark.asyncio
async def test_fetch_falls_back_to_direct_http_when_tinyfish_fails(monkeypatch):
    monkeypatch.setattr(server, "TINYFISH_KEY", "sk-tinyfish-test")
    monkeypatch.setattr(server, "_is_public_url", lambda _url: (True, ""))
    failed = httpx.Response(
        502,
        json={"errors": [{"message": "provider down"}]},
        request=httpx.Request("POST", server.TINYFISH_FETCH_URL),
    )
    html = httpx.Response(
        200,
        headers={"content-type": "text/html; charset=utf-8"},
        text="<html><head><title>Local</title></head><body><main>"
        + ("Readable sentence. " * 20)
        + "</main></body></html>",
        request=httpx.Request("GET", "https://example.com"),
    )

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def post(self, url, headers=None, json=None, **_kwargs):
            assert url == server.TINYFISH_FETCH_URL
            return failed

        async def get(self, url, **_kwargs):
            assert url == "https://example.com"
            return html

    monkeypatch.setattr(server, "_client", lambda: FakeClient())
    body = client.post("/api/fetch", json={"url": "https://example.com"}).json()
    assert body["title"] == "Local"
    assert "Readable sentence" in body["text"]
    assert body["gated"] is False


@pytest.mark.asyncio
async def test_search_prefers_user_tavily_key(monkeypatch):
    monkeypatch.setattr(server, "SERPER_KEY", "server-serper")
    monkeypatch.setattr(server, "TAVILY_KEY", "")
    response = httpx.Response(
        200,
        json={"answer": "from tavily", "results": [{"title": "T", "content": "C", "url": "https://t.test"}]},
        request=httpx.Request("POST", server.TAVILY_URL),
    )

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def post(self, url, headers=None, json=None, **_kwargs):
            assert url == server.TAVILY_URL
            assert headers["Authorization"] == "Bearer tvly-user"
            return response

    monkeypatch.setattr(server, "_client", lambda: FakeClient())
    body = client.post("/api/search", json={"query": "weather", "key": "tvly-user"}).json()
    assert body["answer"] == "from tavily"
    assert body["results"][0]["snippet"].startswith("C")


def test_code_agent_disabled_when_turned_off(monkeypatch):
    monkeypatch.setattr(server, "CODE_AGENT_ENABLED", False)
    response = client.post("/api/code", json={"task": "say hi"})
    assert response.status_code == 503
    assert "turned off" in response.json()["detail"]


def test_code_agent_requires_task_and_grok(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "CODE_AGENT_ENABLED", True)
    assert client.post("/api/code", json={"task": "  "}).status_code == 400

    grok = tmp_path / "grok"
    grok.write_text('#!/bin/sh\necho \'{"text":"done"}\'\n')
    grok.chmod(0o700)
    monkeypatch.setattr(server, "GROK_BIN", grok)

    body = client.post("/api/code", json={"task": "build it"}).json()
    assert body["ok"] is True
    assert body["output"] == "done"


def test_desktop_scroll_blocks_sensitive_scope(monkeypatch, tmp_path):
    _enable_desktop_control(monkeypatch, tmp_path)

    async def sensitive_scope(_app=None):
        return "password"

    monkeypatch.setattr(server, "_screen_scope_looks_sensitive", sensitive_scope)
    response = client.post("/api/desktop/act", json={"action": "scroll", "amount": 5})
    assert response.status_code == 451

    response = client.post(
        "/api/desktop/act",
        json={"action": "drag", "coords": [1, 2, 3, 4]},
    )
    assert response.status_code == 451


def test_desktop_scroll_defaults_to_five_lines(monkeypatch, tmp_path):
    _enable_desktop_control(monkeypatch, tmp_path)

    async def safe_scope(_app=None):
        return None

    async def frontmost_chrome():
        return {"app": "Google Chrome", "title": "Article"}

    captured: list[str] = []

    async def fake_harness(script, _timeout):
        captured.append(script)
        return 0, json.dumps({"ok": True, "result": "ok", "changed": [], "verified": False})

    monkeypatch.setattr(server, "_screen_scope_looks_sensitive", safe_scope)
    monkeypatch.setattr(server, "_desktop_frontmost_context", frontmost_chrome)
    monkeypatch.setattr(server, "_run_harness", fake_harness)
    response = client.post("/api/desktop/act", json={"action": "scroll"})
    assert response.status_code == 200
    # Default amount 5 is scaled to full strides (lines x6).
    assert "scroll(dy=-30)" in captured[0]
    # No app given: preamble must focus the frontmost app and center the
    # pointer, otherwise the wheel events silently no-op.
    assert "open_app(app)" in captured[0]


def test_desktop_scroll_without_app_stays_safe_on_login_window(monkeypatch, tmp_path):
    _enable_desktop_control(monkeypatch, tmp_path)

    async def frontmost_login():
        return {"app": "Google Chrome", "title": "Sign in to X"}

    async def login_scope(_app=None):
        return "sign in"

    async def fake_harness(script, _timeout):
        raise AssertionError("harness must not run for blocked scopes")

    monkeypatch.setattr(server, "_desktop_frontmost_context", frontmost_login)
    monkeypatch.setattr(server, "_screen_scope_looks_sensitive", login_scope)
    monkeypatch.setattr(server, "_run_harness", fake_harness)
    response = client.post("/api/desktop/act", json={"action": "scroll"})
    assert response.status_code == 451
