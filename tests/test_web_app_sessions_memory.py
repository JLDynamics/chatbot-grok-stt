import importlib.util
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

WEB_APP_DIR = Path(__file__).resolve().parents[1] / "web_app"
spec = importlib.util.spec_from_file_location("chatbot_web_app_server_sessions", WEB_APP_DIR / "server.py")
assert spec and spec.loader
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)


@pytest.fixture()
def isolated_client(monkeypatch, tmp_path):
    data = tmp_path / "data"
    sessions = data / "sessions"
    sessions.mkdir(parents=True)
    monkeypatch.setattr(server, "CHATBOT_DATA", data)
    monkeypatch.setattr(server, "SESSIONS_DIR", sessions)
    monkeypatch.setattr(server, "PERSONAL_MEMORY_PATH", data / "personal-memory.md")
    monkeypatch.setattr(server, "SESSION_RETENTION_COUNT", 3)
    return TestClient(server.app)


def test_personal_memory_envelope_matches_native_client(isolated_client):
    isolated_client.put("/api/personal-memory", json={"content": "- Nicole is Jack's daughter."})
    body = isolated_client.get("/api/personal-memory").json()
    assert set(body) >= {"content", "max_chars"}
    assert "Nicole is Jack's daughter" in body["content"]
    assert body["max_chars"] >= len(body["content"])
    config = isolated_client.get("/api/config").json()
    assert {"search", "desktopControl", "chatbotUrl"} <= set(config)


def test_personal_memory_round_trip(isolated_client):
    res = isolated_client.put("/api/personal-memory", json={"content": "- Likes tea"})
    assert res.status_code == 200
    body = isolated_client.get("/api/personal-memory").json()
    assert "Likes tea" in body["content"]


def test_personal_memory_dedupes_duplicate_lines(isolated_client):
    isolated_client.put("/api/personal-memory", json={"content": "- Likes tea\n- likes tea\n- Coffee"})
    body = isolated_client.get("/api/personal-memory").json()
    lines = [line.strip().lower() for line in body["content"].splitlines() if line.strip()]
    assert lines.count("- likes tea") == 1
    assert "- coffee" in lines


def test_legacy_migration_skips_rename_when_profile_too_long(isolated_client, tmp_path, monkeypatch):
    data = tmp_path / "data"
    legacy = data / "memories.json"
    legacy.write_text(json.dumps([{"id": 1, "text": "This legacy fact cannot fit.", "created": "2026-08-12"}]))
    monkeypatch.setattr(server, "PERSONAL_MEMORY_MAX_CHARS", 20)
    isolated_client.get("/api/personal-memory")
    assert legacy.exists()
    assert not legacy.with_name("memories.json.migrated").exists()


def test_legacy_memories_migrate_into_profile(isolated_client, tmp_path):
    data = tmp_path / "data"
    legacy = data / "memories.json"
    legacy.write_text(json.dumps([{"id": 1, "text": "Nicole is Jack's daughter.", "created": "2026-08-12"}]))
    body = isolated_client.get("/api/personal-memory").json()
    assert "Nicole is Jack's daughter" in body["content"]
    assert not legacy.exists()
    assert legacy.with_name("memories.json.migrated").exists()


def test_session_list_shape_matches_native_client(isolated_client):
    created = isolated_client.post("/api/sessions", json={"title": "hello how you"}).json()["session"]
    isolated_client.patch(
        f"/api/sessions/{created['id']}",
        json={"messages": [{"role": "user", "text": "hello"}]},
    )
    listed = isolated_client.get("/api/sessions").json()
    item = listed["sessions"][0]
    for key in ("id", "title", "preview", "message_count", "updated_at"):
        assert key in item
    assert item["title"] == "hello how you"
    assert item["preview"] == "hello"
    assert item["message_count"] == 1


def test_session_crud_and_search(isolated_client):
    created = isolated_client.post("/api/sessions", json={"title": "Audit"}).json()["session"]
    sid = created["id"]
    isolated_client.patch(
        f"/api/sessions/{sid}",
        json={"messages": [{"role": "user", "text": "hello audit"}]},
    )
    hits = isolated_client.get("/api/history/search", params={"q": "audit"}).json()["results"]
    assert any(hit["session_id"] == sid for hit in hits)
    assert isolated_client.delete(f"/api/sessions/{sid}").status_code == 200


def test_session_retention_prunes_oldest(isolated_client):
    ids = []
    for index in range(5):
        session = isolated_client.post("/api/sessions", json={"title": f"S{index}"}).json()["session"]
        ids.append(session["id"])
    listed = isolated_client.get("/api/sessions").json()["sessions"]
    assert len(listed) == 3
    remaining = {item["id"] for item in listed}
    assert ids[-3:] == [item for item in ids if item in remaining]


def test_memories_api_removed(isolated_client):
    assert isolated_client.get("/api/memories").status_code == 404


def test_remember_appends_a_fact_once(isolated_client):
    first = isolated_client.post("/api/personal-memory/remember", json={"fact": "- Jack works at Costco"})
    assert first.status_code == 200
    assert first.json() == {"ok": True, "changed": True, "message": "Saved to the personal profile."}

    again = isolated_client.post("/api/personal-memory/remember", json={"fact": "jack works at costco"})
    assert again.json()["changed"] is False
    assert "already" in again.json()["message"]

    content = isolated_client.get("/api/personal-memory").json()["content"]
    assert content.splitlines() == ["- Jack works at Costco"]

    assert isolated_client.post("/api/personal-memory/remember", json={"fact": "   "}).status_code == 400


def test_remember_refuses_when_the_profile_is_full(isolated_client, monkeypatch):
    monkeypatch.setattr(server, "PERSONAL_MEMORY_MAX_CHARS", 30)
    isolated_client.put("/api/personal-memory", json={"content": "- Jack likes strong black tea"})
    response = isolated_client.post("/api/personal-memory/remember", json={"fact": "Jack has a dog named Biscuit"})
    assert response.status_code == 400
    assert "consolidate" in response.json()["detail"]


def test_forget_removes_matching_lines_only(isolated_client):
    isolated_client.put("/api/personal-memory", json={"content": "- Jack likes tea\n- Jack has a dog\n- Nicole is 12"})

    removed = isolated_client.post("/api/personal-memory/forget", json={"memory": "DOG"})
    assert removed.json() == {"ok": True, "removed": 1, "message": "Removed it from the personal profile."}
    content = isolated_client.get("/api/personal-memory").json()["content"]
    assert content.splitlines() == ["- Jack likes tea", "- Nicole is 12"]

    nothing = isolated_client.post("/api/personal-memory/forget", json={"memory": "cat"})
    assert nothing.json()["removed"] == 0
    assert "No matching" in nothing.json()["message"]

    short = isolated_client.post("/api/personal-memory/forget", json={"memory": "ab"})
    assert short.status_code == 400
    assert "three characters" in short.json()["detail"]
