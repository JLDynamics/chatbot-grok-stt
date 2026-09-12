"""Sessions + memory concern: durable transcripts, history search, personal memory.

Owns the conversation-data paths and the ``history_lock`` serializing
read-modify-write cycles on them.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from web_app import common

router = APIRouter()

# Durable conversation data lives beside the chatbot's existing local settings,
# never inside a code repository.  Full transcripts are the source of truth;
# the Markdown files are small, editable guides that can safely be included in
# a new model session.
CHATBOT_DATA = Path(
    os.path.expanduser(os.environ.get("CHATBOT_DATA_DIR", os.environ.get("S2S_DATA_DIR", "~/.chatbot")))
)
SESSIONS_DIR = CHATBOT_DATA / "sessions"
PERSONAL_MEMORY_PATH = CHATBOT_DATA / "personal-memory.md"
PERSONAL_MEMORY_MAX_CHARS = 10_000  # roughly 2,500 English-language tokens
SESSION_RETENTION_COUNT = max(1, int(common._env_float("CHATBOT_SESSION_RETENTION", 50.0)))
history_lock = asyncio.Lock()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _atomic_write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(value, encoding="utf-8")
    temporary.replace(path)


def _read_json(path: Path, default: object) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _session_path(session_id: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{32}", session_id):
        raise HTTPException(status_code=400, detail="Invalid session.")
    return SESSIONS_DIR / f"{session_id}.json"


def _session_summary(session: dict) -> dict:
    messages = session.get("messages", [])
    preview = next((str(m.get("text", "")).strip() for m in messages if str(m.get("text", "")).strip()), "")
    return {
        "id": session.get("id"),
        "title": session.get("title", "New conversation"),
        "created_at": session.get("created_at"),
        "updated_at": session.get("updated_at"),
        "message_count": len(messages),
        "preview": preview[:180],
    }


def _load_session(session_id: str) -> dict:
    value = _read_json(_session_path(session_id), None)
    if not isinstance(value, dict):
        raise HTTPException(status_code=404, detail="Session not found.")
    value.setdefault("messages", [])
    return value


def _save_session(session: dict) -> None:
    _atomic_write(_session_path(str(session["id"])), json.dumps(session, indent=2, ensure_ascii=False))
    _prune_old_sessions()


def _session_recency_key(path: Path, session: dict) -> tuple[str, str, float, str]:
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0
    return (
        str(session.get("updated_at", "")),
        str(session.get("created_at", "")),
        mtime,
        str(session.get("id", "")),
    )


def _prune_old_sessions() -> None:
    if not SESSIONS_DIR.exists():
        return
    paths = list(SESSIONS_DIR.glob("*.json"))
    # Ranking has to parse every stored session, and this runs on each save.
    # At or under the cap nothing is prunable (unreadable files are skipped, so
    # the ranked list is never longer than this), so skip the reads entirely.
    if len(paths) <= SESSION_RETENTION_COUNT:
        return
    ranked: list[tuple[tuple[str, str, float, str], Path]] = []
    for path in paths:
        value = _read_json(path, None)
        if isinstance(value, dict):
            ranked.append((_session_recency_key(path, value), path))
    ranked.sort(key=lambda item: item[0], reverse=True)
    for _, path in ranked[SESSION_RETENTION_COUNT:]:
        path.unlink(missing_ok=True)


def _legacy_memory_paths() -> list[Path]:
    configured = os.environ.get("CHATBOT_MEMORIES_PATH", os.environ.get("S2S_MEMORIES_PATH", "")).strip()
    paths = [CHATBOT_DATA / "memories.json", CHATBOT_DATA / "memories.json.bak"]
    if configured:
        paths.insert(0, Path(os.path.expanduser(configured)))
    seen: set[Path] = set()
    unique: list[Path] = []
    for path in paths:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(path)
    return unique


def _read_legacy_memories(path: Path) -> list[dict]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []
    return value if isinstance(value, list) else []


def _dedupe_profile_lines(content: str) -> str:
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    seen: set[str] = set()
    unique: list[str] = []
    for line in lines:
        key = line.lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(line)
    return "\n".join(unique)


def _migrate_legacy_memories() -> None:
    """Fold legacy memories.json (and .bak) into personal-memory.md once."""
    profile = PERSONAL_MEMORY_PATH.read_text(encoding="utf-8") if PERSONAL_MEMORY_PATH.exists() else ""
    lines = [line for line in profile.splitlines() if line.strip()]
    existing = {line.strip().lower() for line in lines}
    added = False
    pending_renames: list[Path] = []
    for path in _legacy_memory_paths():
        if not path.exists():
            continue
        path_added = False
        for item in _read_legacy_memories(path):
            text = str(item.get("text", "")).strip()
            if not text:
                continue
            bullet = f"- {text}" if not text.startswith("-") else text
            if bullet.lower() in existing:
                continue
            lines.append(bullet)
            existing.add(bullet.lower())
            added = True
            path_added = True
        if path_added:
            pending_renames.append(path)
    if not added:
        return
    content = _dedupe_profile_lines("\n".join(lines))
    if not content:
        return
    if len(content) > PERSONAL_MEMORY_MAX_CHARS:
        return
    _atomic_write(PERSONAL_MEMORY_PATH, content + "\n")
    for path in pending_renames:
        migrated = path.with_name(f"{path.name}.migrated")
        try:
            path.rename(migrated)
        except OSError:
            path.unlink(missing_ok=True)


class SessionCreateRequest(BaseModel):
    title: str = "New conversation"


class SessionUpdateRequest(BaseModel):
    title: str | None = None
    messages: list[dict] | None = None


class ProfileUpdateRequest(BaseModel):
    content: str


@router.get("/api/sessions")
async def list_sessions() -> JSONResponse:
    async with history_lock:
        sessions = []
        for path in SESSIONS_DIR.glob("*.json") if SESSIONS_DIR.exists() else []:
            value = _read_json(path, None)
            if isinstance(value, dict):
                sessions.append(_session_summary(value))
        sessions.sort(key=lambda item: item.get("updated_at", ""), reverse=True)
    return JSONResponse({"sessions": sessions})


@router.post("/api/sessions")
async def create_session(req: SessionCreateRequest) -> JSONResponse:
    session = {
        "id": uuid.uuid4().hex,
        "title": req.title.strip()[:120] or "New conversation",
        "created_at": _utc_now(),
        "updated_at": _utc_now(),
        "messages": [],
    }
    async with history_lock:
        _save_session(session)
    return JSONResponse({"session": session})


@router.get("/api/sessions/{session_id}")
async def get_session(session_id: str) -> JSONResponse:
    async with history_lock:
        return JSONResponse({"session": _load_session(session_id)})


@router.patch("/api/sessions/{session_id}")
async def update_session(session_id: str, req: SessionUpdateRequest) -> JSONResponse:
    async with history_lock:
        session = _load_session(session_id)
        if req.title is not None:
            session["title"] = req.title.strip()[:120] or "New conversation"
        if req.messages is not None:
            # Transcript messages are plain data, but each text is unbounded.
            # Cap every text so one broken client cannot fill the disk.
            capped = []
            for message in req.messages[-2000:]:
                message = dict(message)
                message["text"] = str(message.get("text", ""))[:20_000]
                capped.append(message)
            session["messages"] = capped
        session["updated_at"] = _utc_now()
        _save_session(session)
    return JSONResponse({"session": session})


@router.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str) -> JSONResponse:
    async with history_lock:
        path = _session_path(session_id)
        if not path.exists():
            raise HTTPException(status_code=404, detail="Session not found.")
        path.unlink()
    return JSONResponse({"ok": True})


@router.get("/api/history/search")
async def search_history(q: str = "", limit: int = 8) -> JSONResponse:
    terms = [term.lower() for term in re.findall(r"[\w'-]+", q) if len(term) > 1][:12]
    if not terms:
        return JSONResponse({"results": []})
    results: list[dict] = []
    async with history_lock:
        for path in SESSIONS_DIR.glob("*.json") if SESSIONS_DIR.exists() else []:
            session = _read_json(path, None)
            if not isinstance(session, dict):
                continue
            for index, message in enumerate(session.get("messages", [])):
                text = str(message.get("text", ""))
                lowered = text.lower()
                score = sum(lowered.count(term) for term in terms)
                if not score:
                    continue
                first = min((lowered.find(term) for term in terms if term in lowered), default=0)
                start, end = max(0, first - 140), min(len(text), first + 360)
                results.append(
                    {
                        "session_id": session.get("id"),
                        "title": session.get("title", "New conversation"),
                        "role": message.get("role", "assistant"),
                        "text": text[start:end],
                        "score": score,
                        "updated_at": session.get("updated_at", ""),
                    }
                )
    results.sort(key=lambda item: (item["score"], item["updated_at"]), reverse=True)
    return JSONResponse({"results": results[: max(1, min(limit, 20))]})


@router.get("/api/personal-memory")
async def get_personal_memory() -> JSONResponse:
    async with history_lock:
        _migrate_legacy_memories()
        content = PERSONAL_MEMORY_PATH.read_text(encoding="utf-8") if PERSONAL_MEMORY_PATH.exists() else ""
    return JSONResponse({"content": content, "max_chars": PERSONAL_MEMORY_MAX_CHARS})


@router.put("/api/personal-memory")
async def put_personal_memory(req: ProfileUpdateRequest) -> JSONResponse:
    async with history_lock:
        _migrate_legacy_memories()
        content = _dedupe_profile_lines(req.content.strip())
        if len(content) > PERSONAL_MEMORY_MAX_CHARS:
            raise HTTPException(status_code=400, detail="Personal memory is too long; consolidate it first.")
        _atomic_write(PERSONAL_MEMORY_PATH, content + ("\n" if content else ""))
    return JSONResponse({"content": content, "max_chars": PERSONAL_MEMORY_MAX_CHARS})


class RememberRequest(BaseModel):
    fact: str


class ForgetRequest(BaseModel):
    memory: str


def _profile_text_locked() -> str:
    _migrate_legacy_memories()
    return PERSONAL_MEMORY_PATH.read_text(encoding="utf-8") if PERSONAL_MEMORY_PATH.exists() else ""


@router.post("/api/personal-memory/remember")
async def remember_fact(req: RememberRequest) -> JSONResponse:
    """Append one fact to the profile (the model's ``remember`` tool).

    Read-modify-write under the history lock, so two tool calls in one turn
    cannot clobber each other the way a client-side GET/PUT pair could.
    """
    fact = re.sub(r"^[-*]\s*", "", req.fact.strip())
    if not fact:
        raise HTTPException(status_code=400, detail="No fact provided.")
    async with history_lock:
        current = _profile_text_locked()
        needle = fact.lower()
        if any(needle in line.strip().lower() for line in current.splitlines()):
            return JSONResponse({"ok": True, "changed": False, "message": "That is already in the personal profile."})
        content = _dedupe_profile_lines("\n".join(part for part in (current.strip(), f"- {fact}") if part))
        if len(content) > PERSONAL_MEMORY_MAX_CHARS:
            raise HTTPException(
                status_code=400, detail="The personal profile is full; consolidate it before adding more."
            )
        _atomic_write(PERSONAL_MEMORY_PATH, content + "\n")
    return JSONResponse({"ok": True, "changed": True, "message": "Saved to the personal profile."})


@router.post("/api/personal-memory/forget")
async def forget_memory(req: ForgetRequest) -> JSONResponse:
    """Remove every profile line mentioning ``memory`` (the model's ``forget`` tool)."""
    needle = req.memory.strip().lower()
    if len(needle) < 3:
        raise HTTPException(
            status_code=400,
            detail="Provide at least three characters so forget does not remove unrelated profile lines.",
        )
    async with history_lock:
        current = _profile_text_locked()
        lines = [line for line in current.splitlines() if line.strip()]
        kept = [line for line in lines if needle not in line.lower()]
        removed = len(lines) - len(kept)
        if removed:
            content = "\n".join(kept)
            _atomic_write(PERSONAL_MEMORY_PATH, content + ("\n" if content else ""))
    message = "Removed it from the personal profile." if removed else "No matching personal memory found."
    return JSONResponse({"ok": True, "removed": removed, "message": message})
