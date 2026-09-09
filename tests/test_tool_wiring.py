"""Contract checks that each Voice tool has a live sidecar endpoint.

Live calls run only when the sidecar is already up, so unit CI stays offline.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

WEB_APP_DIR = Path(__file__).resolve().parents[1] / "web_app"
spec = importlib.util.spec_from_file_location("chatbot_web_app_server_tools", WEB_APP_DIR / "server.py")
assert spec and spec.loader
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)
client = TestClient(server.app)

SIDECAR = os.environ.get("SIDECAR", "http://127.0.0.1:7860/api")


def test_config_exposes_tool_availability():
    body = client.get("/api/config").json()
    assert {"search", "codeAgent", "desktopControl", "chatbotUrl"} <= set(body)


def test_chrome_bridge_reports_disconnected_without_extension():
    status = client.get("/api/browser/status").json()
    assert "connected" in status
    response = client.post("/api/browser/read")
    assert (
        response.status_code in {404, 409, 422, 503}
        or (response.status_code == 200 and response.json().get("status") == "failed")
        or (
            isinstance(response.json().get("detail"), dict)
            and response.json()["detail"].get("reason")
            in {
                "bridge_never_enabled",
                "bridge_expired",
            }
        )
    )


def sidecar_up() -> bool:
    try:
        return httpx.get(f"{SIDECAR}/config", timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


@pytest.mark.skipif(not sidecar_up(), reason="sidecar not running")
def test_live_search_fetch_code_and_screenshot():
    search = httpx.post(f"{SIDECAR}/search", json={"query": "OpenAI"}, timeout=20)
    assert search.status_code == 200, search.text
    assert search.json().get("results")

    fetched = httpx.post(f"{SIDECAR}/fetch", json={"url": "https://example.com"}, timeout=20)
    assert fetched.status_code == 200, fetched.text
    body = fetched.json()
    assert "Example" in (body.get("title") or "") or (body.get("text") or "")

    code = httpx.post(f"{SIDECAR}/code", json={"task": "echo tool-wiring-ok"}, timeout=30)
    assert code.status_code == 200, code.text
    assert "tool-wiring-ok" in (code.json().get("output") or "")

    shot = httpx.post(f"{SIDECAR}/desktop/act", json={"action": "screenshot"}, timeout=30)
    assert shot.status_code in {200, 403, 451}, shot.text
    if shot.status_code == 200:
        assert shot.json().get("image", "").startswith("data:image/png;base64,")
    elif shot.status_code == 451:
        assert "sign-in" in shot.json()["detail"].lower() or "payment" in shot.json()["detail"].lower()
    else:
        assert "Screen Recording" in shot.json()["detail"]

    bridge = httpx.post(f"{SIDECAR}/browser/read", timeout=10)
    detail = bridge.json().get("detail")
    if isinstance(detail, dict):
        assert detail.get("reason") in {"bridge_never_enabled", "bridge_expired"} or bridge.status_code == 200
    else:
        assert bridge.status_code in {200, 404, 409, 503}
