import importlib
import json
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

DEMO_DIR = Path(__file__).resolve().parents[1] / "demo"
sys.path.insert(0, str(DEMO_DIR))
demo_auth = importlib.import_module("auth")
demo_server = importlib.import_module("server")


@pytest.fixture(autouse=True)
def clear_browser_article_bridge():
    demo_server._browser_articles.clear()
    yield
    demo_server._browser_articles.clear()


async def test_read_article_prefers_fresh_x_dom_body_and_excludes_replies(monkeypatch):
    body = "Everyone is racing to be successful right now.\n" + ("Strategy compounds. " * 50)
    article = demo_server.BrowserArticle(
        url="https://x.com/thedankoe/status/2086197754452377955",
        article_id="2086197754452377955",
        title="The Art Of Strategic Thinking",
        text=body,
        content_type="x_article",
        complete=True,
        comments_excluded=True,
        replies_detected=True,
        boundary="x_article_body_end",
    )
    demo_server._store_browser_article(article)
    monkeypatch.setattr(
        demo_server,
        "_run_harness",
        lambda *args, **kwargs: pytest.fail("fresh DOM article must bypass screenshots"),
    )

    response = await demo_server.read_article(demo_server.ArticleRequest(app="Google Chrome"))
    payload = json.loads(response.body)

    assert payload["source"] == "chrome_bridge"
    assert payload["text"] == body
    assert payload["complete"] is True
    assert payload["comments_excluded"] is True
    assert payload["replies_detected"] is True
    assert payload["boundary"] == "x_article_body_end"
    assert "Amazing article, Dan" not in payload["text"]


async def test_read_article_prefers_generic_browser_page_without_harness(monkeypatch):
    body = "A universal bridge can extract a semantic article body.\n" + ("Useful context. " * 40)
    page = demo_server.BrowserArticle(
        url="https://example.com/news/browser-bridge",
        title="A safer browser bridge",
        text=body,
        source="browser_dom",
        content_type="article",
        complete=True,
        clutter_filtered=True,
        comments_excluded=True,
        boundary="semantic_article_end",
    )
    demo_server._store_browser_page(page)
    monkeypatch.setattr(
        demo_server,
        "_run_harness",
        lambda *args, **kwargs: pytest.fail("browser DOM text must bypass desktop control"),
    )

    response = await demo_server.read_article(demo_server.ArticleRequest())
    payload = json.loads(response.body)

    assert payload["source"] == "chrome_bridge"
    assert payload["content_type"] == "article"
    assert payload["text"] == body
    assert payload["clutter_filtered"] is True
    assert payload["boundary"] == "semantic_article_end"


def test_browser_article_bridge_rejects_non_x_pages():
    article = demo_server.BrowserArticle(
        url="https://example.com/private",
        text="x" * 700,
        complete=True,
        comments_excluded=True,
        boundary="x_article_body_end",
    )

    with pytest.raises(demo_server.HTTPException) as error:
        demo_server._store_browser_article(article)

    assert error.value.status_code == 400


@pytest.mark.parametrize("url", [
    "file:///Users/jack/private.txt",
    "chrome://settings/",
    "http://127.0.0.1:7860/",
])
def test_browser_page_bridge_rejects_unsafe_or_self_referential_pages(url):
    page = demo_server.BrowserArticle(
        url=url,
        text="x" * 700,
        source="browser_dom",
        complete=True,
        boundary="main_content_end",
    )

    with pytest.raises(demo_server.HTTPException) as error:
        demo_server._store_browser_page(page)

    assert error.value.status_code == 400


async def test_read_article_does_not_use_harness_without_explicit_fallback(monkeypatch):
    monkeypatch.setattr(demo_server, "DESKTOP_READ_ENABLED", True)
    monkeypatch.setattr(
        demo_server,
        "_run_harness",
        lambda *args, **kwargs: pytest.fail("desktop fallback was not authorized"),
    )

    with pytest.raises(demo_server.HTTPException) as error:
        await demo_server.read_article(demo_server.ArticleRequest())

    assert error.value.status_code == 503


async def test_browser_page_endpoint_requires_extension_worker_header():
    payload = {
        "url": "https://example.com/news/safe-page",
        "title": "Safe page",
        "text": "Main article text. " * 40,
        "source": "browser_dom",
        "content_type": "article",
        "complete": True,
        "boundary": "semantic_article_end",
    }
    transport = httpx.ASGITransport(app=demo_server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        rejected = await client.post("/api/browser/page", json=payload)
        accepted = await client.post(
            "/api/browser/page",
            json=payload,
            headers={"X-Chatbot-Bridge": "page-v1"},
        )

    assert rejected.status_code == 403
    assert accepted.status_code == 200
    assert accepted.json()["chars"] == len(payload["text"])


def test_browser_article_bridge_expires_background_tab_data():
    article = demo_server.BrowserArticle(
        url="https://x.com/user/article/123",
        article_id="123",
        text="x" * 700,
        complete=True,
        comments_excluded=True,
        boundary="x_focus_article_end",
    )
    demo_server._browser_articles["123"] = (time.monotonic() - 600, article)

    assert demo_server._fresh_browser_article() is None
    assert demo_server._browser_articles == {}




def test_current_access_token_normalizes_oauth_token(monkeypatch):
    monkeypatch.setattr(
        demo_auth,
        "current_oauth",
        lambda request: {"access_token": "  hf_user_token  "},
    )

    assert demo_auth.current_access_token(object()) == "hf_user_token"


def test_current_access_token_rejects_missing_or_empty_token(monkeypatch):
    monkeypatch.setattr(demo_auth, "current_oauth", lambda request: None)
    assert demo_auth.current_access_token(object()) is None

    monkeypatch.setattr(demo_auth, "current_oauth", lambda request: {"access_token": "  "})
    assert demo_auth.current_access_token(object()) is None


def test_expiring_oauth_token_requires_fresh_login(monkeypatch):
    monkeypatch.setattr(
        demo_auth,
        "current_oauth",
        lambda request: {
            "access_token": "hf_user_token",
            "access_token_expires_at": datetime.now() + timedelta(seconds=15),
            "user_info": {"sub": "123", "preferred_username": "alice"},
        },
    )

    assert demo_auth.current_access_token(object()) is None
    assert demo_auth.oauth_login_required_reason(object()) == "token_expired"
    assert demo_auth.user_view(object()) == {
        "loggedIn": False,
        "tier": "anon",
        "reason": "token_expired",
    }


def test_load_balancer_headers_forward_signed_in_user_token(monkeypatch):
    monkeypatch.setattr(
        demo_server.auth,
        "current_access_token",
        lambda request: "hf_user_token",
    )

    assert demo_server._load_balancer_headers(object()) == {
        "Content-Type": "application/json",
        "User-Agent": "chatbot-demo",
        "X-Reachy-Mini-Authorization": "Bearer hf_user_token",
    }


def test_load_balancer_headers_keep_anonymous_requests_credential_free(monkeypatch):
    monkeypatch.setattr(demo_server.auth, "current_access_token", lambda request: None)

    headers = demo_server._load_balancer_headers(object())

    assert headers == {
        "Content-Type": "application/json",
        "User-Agent": "chatbot-demo",
    }


async def test_session_preserves_load_balancer_authentication_failure(monkeypatch):
    class FakeAsyncClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, *args, **kwargs):
            return httpx.Response(401, json={"reason": "token_invalid"})

    monkeypatch.setattr(demo_server, "LOAD_BALANCER_URL", "https://load-balancer.example")
    monkeypatch.setattr(demo_server, "AUTH_ENABLED", True)
    monkeypatch.setattr(demo_server, "LIMITER_ENABLED", False)
    monkeypatch.setattr(demo_server.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(demo_server.auth, "oauth_login_required_reason", lambda request: None)
    monkeypatch.setattr(demo_server.auth, "resolve_identity", lambda request: ("free", ["key"], None))
    monkeypatch.setattr(demo_server.auth, "current_access_token", lambda request: "hf_user_token")

    request = SimpleNamespace(scope={"session": {"oauth_info": {"access_token": "hf_user_token"}}})
    response = await demo_server.session(request)

    assert response.status_code == 401
    assert json.loads(response.body) == {
        "reason": "token_invalid",
        "loginUrl": demo_auth.OAUTH_LOGIN_PATH,
    }
    assert "oauth_info" not in request.scope["session"]


async def test_session_rejects_locally_expired_oauth_before_proxying(monkeypatch):
    monkeypatch.setattr(demo_server, "LOAD_BALANCER_URL", "https://load-balancer.example")
    monkeypatch.setattr(demo_server, "AUTH_ENABLED", True)
    monkeypatch.setattr(
        demo_server.auth,
        "oauth_login_required_reason",
        lambda request: "token_expired",
    )

    response = await demo_server.session(object())

    assert response.status_code == 401
    assert json.loads(response.body) == {
        "reason": "token_expired",
        "loginUrl": demo_auth.OAUTH_LOGIN_PATH,
    }


async def test_queue_rejects_locally_expired_oauth_before_polling(monkeypatch):
    monkeypatch.setattr(demo_server, "LOAD_BALANCER_URL", "https://load-balancer.example")
    monkeypatch.setattr(demo_server, "AUTH_ENABLED", True)
    monkeypatch.setattr(
        demo_server.auth,
        "oauth_login_required_reason",
        lambda request: "token_expired",
    )
    monkeypatch.setattr(
        demo_server.auth,
        "resolve_identity",
        lambda request: pytest.fail("expired OAuth must not resolve as anonymous"),
    )

    response = await demo_server.queue_status("ticket", object())

    assert response.status_code == 401
    assert json.loads(response.body) == {
        "reason": "token_expired",
        "loginUrl": demo_auth.OAUTH_LOGIN_PATH,
    }


def test_websocket_client_classifies_session_401_as_login_required():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for demo client tests")

    script = """
globalThis.localStorage = { getItem() { return null; } };
globalThis.fetch = async () => ({
  status: 401,
  ok: false,
  async json() {
    return { reason: "token_invalid", loginUrl: "/oauth/huggingface/login" };
  },
});
const { S2sWsRealtimeClient } = await import("./demo/ws/s2s-ws-client.js");
const client = new S2sWsRealtimeClient({
  voice: "Aiden",
  instructions: "Be helpful.",
  sessionUrl: "api/session",
});
try {
  await client._postSession();
  throw new Error("expected the session request to fail");
} catch (error) {
  if (error.code !== "login-required") throw error;
  if (error.loginUrl !== "/oauth/huggingface/login") {
    throw new Error(`unexpected login URL: ${error.loginUrl}`);
  }
}
"""
    subprocess.run(
        [node, "--input-type=module", "-e", script],
        cwd=Path(__file__).resolve().parents[1],
        check=True,
        capture_output=True,
        text=True,
    )


def test_login_required_state_wins_over_inflight_account_refresh():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for demo client tests")

    script = """
globalThis.localStorage = { getItem() { return null; } };
const elements = new Map();
function element() {
  return {
    hidden: false,
    innerHTML: "",
    textContent: "",
    href: "",
    open: false,
    addEventListener() {},
    contains() { return false; },
    setAttribute() {},
    showModal() { this.open = true; },
    close() { this.open = false; },
  };
}
globalThis.document = {
  querySelector(selector) {
    if (!elements.has(selector)) elements.set(selector, element());
    return elements.get(selector);
  },
  addEventListener() {},
  getElementById(id) { return this.querySelector(`#${id}`); },
};

let resolveFetch;
globalThis.fetch = () => new Promise((resolve) => { resolveFetch = resolve; });

const { Account } = await import("./demo/ui/account.js");
const account = new Account();
const refresh = account.refresh();
account.showLoginRequired("/oauth/huggingface/login");
resolveFetch({
  ok: true,
  async json() {
    return {
      enabled: true,
      auth: true,
      loggedIn: true,
      username: "alice",
      tier: "free",
      loginUrl: "/oauth/huggingface/login",
    };
  },
});
await refresh;

if (account.tier !== "anon") throw new Error(`unexpected tier: ${account.tier}`);
const root = elements.get("#account");
if (!root.innerHTML.includes("signin-pill")) {
  throw new Error(`stale account refresh won: ${root.innerHTML}`);
}
"""
    subprocess.run(
        [node, "--input-type=module", "-e", script],
        cwd=Path(__file__).resolve().parents[1],
        check=True,
        capture_output=True,
        text=True,
    )
