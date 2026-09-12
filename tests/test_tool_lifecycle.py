import asyncio
import importlib.util
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from web_app import desktop as desktop_module

spec = importlib.util.spec_from_file_location(
    "tool_lifecycle_server", Path(__file__).resolve().parents[1] / "web_app" / "server.py"
)
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)


class Connection:
    def __init__(self, disconnected=False):
        self.disconnected = disconnected

    async def is_disconnected(self):
        return self.disconnected


async def test_client_disconnect_cancels_pending_work():
    cancelled = asyncio.Event()

    async def pending():
        try:
            await asyncio.sleep(30)
        finally:
            cancelled.set()

    with pytest.raises(asyncio.CancelledError):
        await desktop_module._while_connected(pending(), Connection(True), 1)
    assert cancelled.is_set()


async def test_tool_timeout_cleans_up_work():
    cancelled = asyncio.Event()

    async def pending():
        try:
            await asyncio.sleep(30)
        finally:
            cancelled.set()

    with pytest.raises(asyncio.TimeoutError):
        await desktop_module._while_connected(pending(), Connection(), 0.01)
    assert cancelled.is_set()


async def test_completed_tool_result_is_preserved():
    async def completed():
        return "result"

    assert await desktop_module._while_connected(completed(), Connection(), 1) == "result"


@pytest.mark.parametrize("output", ['{"ok": false}', "not-json", "[]"])
def test_desktop_does_not_claim_success_for_failed_or_invalid_result(monkeypatch, tmp_path, output):
    harness = tmp_path / "harness"
    harness.write_text("#!/bin/sh\n")
    harness.chmod(0o700)
    monkeypatch.setattr(desktop_module, "DESKTOP_HARNESS_BIN", harness)
    monkeypatch.setattr(desktop_module, "DESKTOP_CONTROL_ENABLED", True)

    async def safe(_):
        return None

    async def run(_, _timeout):
        return 0, output

    monkeypatch.setattr(desktop_module, "_screen_scope_looks_sensitive", safe)
    monkeypatch.setattr(desktop_module, "_run_harness", run)
    response = TestClient(server.app).post("/api/desktop/act", json={"action": "scroll"})
    assert response.status_code == 502
