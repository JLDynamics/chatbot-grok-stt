# Repository Guidelines

## Project Structure & Module Organization

- `src/chatbot/`: Python voice pipeline, including `STT/`, `TTS/`, `VAD/`, `LLM/`, and `api/openai_realtime/`.
- `web_app/server.py`: FastAPI sidecar for tools, sessions, and memory. `web_app/chrome_article_bridge/` contains the Chrome extension; there is no active browser chat UI.
- `macos/Voice/Sources/`: native SwiftUI panel, organized into `App/`, `UI/`, `Session/`, and `Audio/`. App metadata and entitlements live in `Resources/`.
- `tests/`: Python tests, with realtime and tool-call suites in subdirectories. `.github/workflows/` defines CI and publishing.

## Build, Test, and Development Commands

Use an Apple Silicon Mac with Python 3.10+, `uv`, and Xcode command-line tools.

- `uv sync --group dev`: install runtime and development dependencies.
- `./set-keys.sh`: configure local API keys.
- `./run-browser.sh`: start the realtime backend on port 8766 and API sidecar on port 7860.
- `./macos/Voice/scripts/build.sh`: compile and sign the native app; launch with `open macos/Voice/build/Voice.app`.
- `uv run pytest tests/ -q`: run the Python test suite.
- `uv run ruff check src/ tests/` and `uv run ruff format --check src/ tests/`: check lint and formatting.
- `uv run mypy src/`: check Python types.
- `uv build`: build Python distribution packages.

## Coding Style & Naming Conventions

Use four-space indentation in Python and Swift, and two spaces in extension JavaScript. Use Python `snake_case` functions/modules and `PascalCase` classes; Swift and JavaScript use `camelCase` members. Follow existing uppercase Python subsystem directories. Ruff uses a 120-character line length and checks imports; apply formatting with `uv run ruff format` on changed Python files.

## Testing Guidelines

Tests use pytest and pytest-asyncio with automatic async mode. Name files `test_*.py` and functions `test_*`. Use `monkeypatch` and `tmp_path` to isolate external tools and persistence. Run focused tests while iterating, then CI checks before submitting. No numerical coverage threshold is configured. For voice or UI changes, build the panel and manually verify the affected interaction; report microphone and live-service checks separately from automated results.

## Commit & Pull Request Guidelines

Use short, imperative commit subjects, following history: “Restore chatbotUrl config field with contract test.” Work on a focused feature branch from `main`. Describe the problem, resulting behavior, and validation; link relevant issues and include screenshots for visible UI changes. Wait for review before merging.

## Security & Configuration

Keep credentials in `~/.config/chatbot/env` and personal data in `~/.chatbot`; never commit either. Preserve unrelated working-tree changes and keep realtime, session, and memory contracts compatible across the native client and Python services.
