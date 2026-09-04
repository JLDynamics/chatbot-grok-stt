# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
uv sync --group dev                  # first run in a fresh checkout/worktree
```

CI (`.github/workflows/ci.yml`) gates on exactly these, in order:

```bash
uv run python -c "import nltk; nltk.download('punkt_tab')"   # LLM sentence batching needs it
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
uv run mypy src/
uv run pytest tests/ -q
```

Single test / single case:

```bash
uv run pytest tests/test_vad_iterator.py -q
uv run pytest tests/test_vad_iterator.py::test_reset_states_clears_sample_counters -q
```

Native macOS panel (no Xcode project — `swiftc` over every file in `Sources/`):

```bash
./macos/Voice/scripts/build.sh /tmp/voice-check/Voice.app
```

Run the stack (backend `:8766` + sidecar `:7860`; `set-keys.sh` writes `~/.config/chatbot/env`):

```bash
./set-keys.sh
./run-browser.sh                     # both processes; run-openrouter.sh is the backend alone
```

Override ports and data dir to avoid colliding with a running everyday instance:

```bash
PORT=8866 WEB_PORT=7960 CHATBOT_DATA_DIR=/tmp/scratch ./run-browser.sh
```

The packaging smoke test is **not** a pytest file — CI builds a wheel, installs it into `.venv-smoke`,
then runs `PATH="$PWD/.venv-smoke/bin:$PATH" .venv-smoke/bin/python tests/install_smoke.py`. It will
not run locally without that venv.

## Architecture

Two processes plus a native client. There is **no browser UI** (removed in the native-only pivot).

```
Voice.app (Swift)  ──ws──►  chatbot serve  :8766   realtime pipeline
        │                   (src/chatbot)
        └──http──►  web_app/server.py :7860        sidecar: sessions, memory, tools, Chrome bridge
```

### The pipeline is threads joined by queues

`s2s_pipeline._build_pipeline_unit` creates 8 `queue.Queue`s and wires a chain of `BaseHandler`
subclasses, one thread each, run by `ThreadManager`:

```
audio bytes → VADHandler → STT (Parakeet MLX) → TranscriptionNotifier
            → LLM (OpenRouter Responses) → LMOutputProcessor → TTS (Kokoro/VibeVoice) → audio out
```

`BaseHandler.run` is the shared loop: pull from `queue_in`, call `process()`, push each yielded item
to `queue_out`. Subclasses override `process`, plus optional hooks (`should_process_input`,
`before_emit_output`, `on_session_end`). To understand any handler, read `baseHandler.py` first.

A `PipelineUnit` (`api/openai_realtime/pipeline_unit.py`) bundles one pipeline's queues, events,
`RealtimeService` and handler list. The WebSocket route claims a free unit on connect and releases it
on disconnect. `websocket_router._send_loop_for` is the per-unit async loop that drains the output
queues and batches audio to the transport.

### Turn-taking is the subtle part — don't regress it

Several mechanisms cooperate; changing one in isolation tends to break barge-in or split turns:

- **`SpeculativeTurnTracker`** (`pipeline/speculative_turns.py`) — every turn has `turn_id` +
  `turn_revision`. A soft-ended turn can *reopen* at a higher revision if the user resumes speaking
  within a grace window, so a thinking pause does not split one sentence into two turns. Handlers
  discard superseded work via `is_latest()` / `commit()`.
- **`CancelScope`** (`pipeline/cancel_scope.py`) — a generation counter. Barge-in bumps it; LLM and
  TTS threads notice their captured generation is stale and stop.
- **Smart Turn** (`VAD/smart_turn.py`) — an ONNX classifier run *only* at a speech→silence boundary,
  deciding whether the utterance sounds finished and therefore how long the reopen grace should be.
- **VAD gating** — `VAD_MIN_SPEECH_MS` of *confident* speech is required before a turn or a barge-in
  is confirmed; short fragments can be held and stitched (`short_segment_merge_ms`).

`git log` around the turn-taking commits explains why the current thresholds exist.

### One GPU, two consumers

STT and TTS both run on MLX/Metal and are serialised by `MLXLockContext` (`utils/mlx_lock.py`). TTS
holds the lock for the whole generation; final STT retries 5s then 25s so barge-in speech is not
dropped. Work that lengthens TTS's lock hold directly delays STT.

### Pluggable backends

`backend_registry.py` maps `--stt` / `--llm_backend` / `--tts` to handler classes.
`arguments_classes/*` are dataclasses parsed by `HfArgumentParser`, so every CLI flag is a field
there. `run-openrouter.sh` is the canonical invocation and the source of truth for default flags.

### Sidecar (`web_app/server.py`, single file)

Sessions and personal memory are JSON/Markdown under `CHATBOT_DATA_DIR` (default `~/.chatbot`).
Also hosts web search/fetch, the Chrome page bridge receiver, and desktop control.

- **Chrome bridge** is **push-only**: `web_app/chrome_article_bridge/` posts page text to
  `/api/browser/page`; the server holds it for `BROWSER_PAGE_TTL_S` (300s) and expires it on read.
  There is no way for the server to *request* a page. `/api/browser/read` returning 503 means no page
  exists — no URL is available in that response. The extension is version-pinned against the server's
  `expected_version`.
- **Desktop control** shells out to an **external CLI**, `~/.local/bin/desktop-harness`, which points
  at a separate repo. It is *not* a Python dependency and is not importable from this venv. The
  sidecar builds a Python script as a string and runs `desktop-harness -c <script>` with
  `DH_NO_DAEMON=1` (deliberate: the daemon's log path and Screen Recording permission are unreliable
  under the web server, at the cost of ~100ms cold start per call).

### Swift panel

`macos/Voice/Sources/`. `Session/LocalService.swift` is the single source of both service URLs, each
overridable at launch (`-voice.wsUrl`, `-voice.sidecarUrl`). `Session/VoiceTools.swift` holds the tool
schemas **and the routing prompt text** the model follows.

## Gotchas

- **`ruff format --check` currently fails on 7 files** and has done since before recent work. It is a
  CI gate, so it will look like you broke it. Verify against the base commit before investigating.
- **CI lints `src/ tests/` only — `web_app/` is not covered** by ruff or mypy. Lint it explicitly
  (`uv run ruff check web_app/`) when you change the sidecar.
- **Some Python tests assert on Swift source as text.** `tests/test_web_app_desktop_control.py` reads
  `VoiceTools.swift` and asserts exact substrings of tool descriptions. Rewording a prompt string
  breaks tests that never mention Swift.
- **Sidecar tests import `web_app/server.py` by path** via `importlib` (it is not a package) and
  isolate state with `monkeypatch.setattr(server, "SESSIONS_DIR", ...)`. Copy the fixture in
  `tests/test_web_app_sessions_memory.py`.
- **Never point tests or dev runs at `~/.chatbot` or `~/.config/chatbot/env`.** Use `CHATBOT_DATA_DIR`.
- **`.cursor/skills/verify-chatbot/` is stale.** It drives a browser UI (`web_app/index.html`) that no
  longer exists. Its `scripts/launch.sh` and isolated-port convention (`17860`/`18766`) are still
  useful; its browser recipes are not.
- `pytest` runs with `asyncio_mode = "auto"` — async tests need no decorator.
- First backend run downloads several GB of MLX models into the shared HuggingFace cache.

## Repository rules (from AGENTS.md)

- Never include `codex` in branch names or pull request titles.
- On existing/open PRs, do not amend, squash, rebase-rewrite, or force-push. Add new commits.
- Do not commit `dist/`, `build/`, or generated wheels/sdists.
- Releases: bump `version` in `pyproject.toml` **and** `__version__` in `src/chatbot/__init__.py` in a
  PR of their own, then tag `vX.Y.Z` on `main` — `.github/workflows/publish.yml` does the PyPI upload.
