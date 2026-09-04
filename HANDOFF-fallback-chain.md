# Handoff: Tool fallback chain (fetch → bridge → desktop)

## Goal
Stop telling user to "reload extension". Make article reading auto-fallback: `web_fetch` → `read_article` (Chrome bridge) → `control_screen screenshot` only as last resort for visible content. Keep strict text-vs-visual routing.

## Context
- `chatbot-opt/macos/Voice/Sources/Session/VoiceTools.swift:24-43` (`toolIntentRouting`): text intent must use `read_article`, visual must use `control_screen screenshot`. `execReadArticle:330-350` currently returns "bridge unavailable, reload page" with no fallback.
- Sidecar `chatbot-opt/web_app/server.py`: `POST /api/fetch:491` (public fetch, 20k chars), `POST /api/browser/read:215` (bridge DOM, 60k chars, 5-min TTL), `POST /api/context/preflight:755` (route_hint only), `POST /api/desktop/act:879` (screenshot/click/scroll), `POST /api/search:375`.
- Harness `desktop-harness/SKILL.md:37-44,66-104`: AX-first (`labels/find/click_text`), screenshot fallback, `open_stage/close_stage` for off-side web, single `run_plan` not N calls.
- Test guard `chatbot-opt/tests/test_web_app_desktop_control.py:6-11` asserts current split (`screenshot` in enum, `use read_article` / `must not use read_article` strings). Must update, not delete.

## Changes (in order)

### 1. Swift: auto-fallback in `execReadArticle`
File: `chatbot-opt/macos/Voice/Sources/Session/VoiceTools.swift:330-350`
- On `browser/read` non-200/empty: first try `POST /api/fetch` if response JSON contains `url` (bridge returns `url` field today — reuse it; if missing, return bridge-unavailable as now).
- Only if fetch also fails AND request was visual/ambiguous: fall back to `control_screen screenshot` path (call existing `execControlScreen` internally, prefix output `Bridge+fetch unavailable, showing visible screen instead:`).
- Pure text intent (`read/summarize article`) must NOT silently screenshot — return `title + fetch text` or clear `bridge+fetch unavailable for <url>` message.
- Update `toolIntentRouting:24-43` + `read_article` description `:117-129` + `control_screen` description `:143-167` to describe new order. Keep strings `use read_article` / `must not use read_article` (test depends on them) or update test together.

### 2. Sidecar: expose URL for fallback (small)
File: `chatbot-opt/web_app/server.py:215-229`
- Ensure `read_browser_page` error response includes fresh page `url` when present (even when stale/empty) so Swift can fetch it. No new endpoint. Keep `_validate_browser_page`, TTL 300s, 60k cap unchanged.

### 3. Tests
- Update `tests/test_web_app_desktop_control.py`: keep schema asserts, add fallback asserts (bridge-500 → fetch attempted; text-intent never screenshots).
- Add `tests/test_read_fallback.py` (new, pure pytest, `monkeypatch` + `TestClient` pattern from `tests/test_web_app_sessions_memory.py:15-24`): bridge-miss → fetch-hit; bridge-miss + fetch-miss → clear message; follow existing `monkeypatch.setattr server.*` style.
- No live keys, no `node`, no real Chrome. Follow `asyncio_mode=auto`.

### 4. Docs (2 lines each)
- `chatbot-opt/README.md` config table + `web_app/chrome_article_bridge/README.md`: note fallback order.
- `.cursor/skills/verify-chatbot/features/README.md`: add recipe `bridge-down → fetch` using `scripts/launch.sh` isolated ports (`17860`/`18766`).

## Non-goals
- No scroll-stitch full-page OCR. No auto-clicking websites. No changes to VAD/STT/LLM/TTS pipeline, ports (`8766`/`7860`), or `DESKTOP_CONTROL` gating.
- No new deps. No Bun/Node sidecar (only legacy `chatbot/package.json` has it).

## Acceptance
- `read_article` with bridge down + public URL returns fetch text, no "reload extension" prompt.
- Text intent never returns screenshot pixels; visual intent can.
- `uv run ruff check src tests && uv run ruff format --check src/ tests/ && uv run mypy src/ && uv run pytest tests/ -q` green (plus `nltk punkt_tab` download as in `ci.yml`).
- `tests/install_smoke.py` run directly (not via pytest) still passes.

## Verify
```bash
cd chatbot-opt
uv sync --group dev
uv run ruff check src tests
uv run mypy src
uv run pytest tests/ -q
PATH="$PWD/.venv-smoke/bin:$PATH" .venv-smoke/bin/python tests/install_smoke.py
```
