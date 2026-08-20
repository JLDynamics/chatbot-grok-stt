# Chatbot

A Mac-first live voice chatbot that runs its ears and voice locally, while a Responses API model handles the conversation.

The retained path is intentionally small:

`browser microphone → Parakeet MLX → Responses API → Sesame CSM-1B MLX → browser speakers`

It keeps realtime WebSocket turn-taking, interruption/cancellation, partial transcripts, long-term memory, web tools, the Chrome page bridge, camera, coding-agent delegation, and explicit desktop control.

The mic stays open while the assistant speaks (full-duplex barge-in). Echo control is the browser's AEC plus Silero VAD — we do not mute capture during TTS. Saved sidebar chats are replayed into the backend on connect (last 20 text turns). Personal memory is injected via instructions. CSM does not apply an AI watermark unless `silentcipher` is installed.

## Requirements

- Apple Silicon Mac
- Python 3.10 or newer
- [uv](https://docs.astral.sh/uv/)
- Node.js, for the optional coding agent
- An OpenRouter key

## Install and run

```bash
uv sync --group dev
npm install
./set-keys.sh
./run-browser.sh
```

Open `http://127.0.0.1:7860`, allow microphone access, and click the orb. The first launch may download the Parakeet and CSM model files.

The default model path is `openai/gpt-5.6-luna` through OpenRouter. Parakeet and Sesame CSM-1B run locally with MLX; the default CSM voice is `conversational_b`.

## Configuration

The launch scripts read secrets from `~/.config/chatbot/env`, written with owner-only permissions by `set-keys.sh`. Environment values override saved values.

| Setting | Default | Purpose |
| --- | --- | --- |
| `MODEL` | `openai/gpt-5.6-luna` | OpenRouter Responses API model ID |
| `CSM_MODEL` | `mlx-community/csm-1b-8bit` | CSM-1B MLX model repo |
| `CSM_VOICE` | `conversational_b` | CSM voice prompt (`conversational_a`/`conversational_b`) |
| `CSM_TEMP` | `0.55` | CSM sampling temperature; lower is steadier |
| `CSM_BATCH_SENTENCES` | `3` | Sentences grouped per CSM generation; higher keeps voice continuity |
| `PORT` / `WEB_PORT` | `8766` / `7860` | Realtime and browser ports |
| `PROMPT` | concise voice prompt | Backend system prompt |
| `STARTUP_GREETING` | empty | Optional greeting instruction on connection |
| `TAVILY_API_KEY` / `SERPER_API_KEY` | empty | Enables the app's local search function |
| `CODE_AGENT` | `on` | Set `off` to hide coding-agent delegation |
| `CODE_AGENT_CWD` | home folder | Default working folder for coding tasks |
| `CODE_AGENT_MODEL` | `openai/gpt-5.6-luna` | OpenRouter model for coding-agent delegation |
| `DESKTOP_CONTROL` | `on` | Server-side kill switch for explicit Mac actions |
| `CHATBOT_MEMORIES_PATH` | `~/.chatbot/memories.json` | Legacy JSON memory file, if used |
| `CHATBOT_WATERMARK` | off | CSM watermarking is not installed (`silentcipher`); leave off unless you accept extra per-chunk latency |
| `CHATBOT_DATA_DIR` | `~/.chatbot` | Saved chats, personal profile, and project notebooks |

### Search, fetch, and the Chrome bridge

These solve different jobs:

- **Local web search**: Tavily (or Serper) returns result titles, short snippets, and URLs to the OpenRouter model when a search key is configured.
- **Web fetch**: reads the bounded text of one known public HTTP(S) URL. It does not discover pages, and it refuses localhost/private-network addresses.
- **Chrome page bridge**: reads bounded, reader-style main text from the visible public webpage, news story, documentation, blog post, dedicated X long-form Article, or individual X status post, including pages that a normal fetch cannot access. It is read-only and excludes X replies/timelines.

## Chrome page bridge

The extension is part of the live product at [`web_app/chrome_article_bridge`](web_app/chrome_article_bridge).

1. Keep the web app on its default `127.0.0.1:7860` address.
2. Open `chrome://extensions`.
3. Enable **Developer mode**.
4. Choose **Load unpacked** and select `web_app/chrome_article_bridge`.
5. Open or reload the chatbot at `http://127.0.0.1:7860`. The extension enables
   itself automatically and shows a green check. Then open or reload the public
   page you want the chatbot to read.

If an older unpacked copy still points to the deleted `demo/chrome_article_bridge`
folder, remove it and load the `web_app` path above. When bridge code changes,
use the extension card's **Reload** button and then reload the article tab.
The current card version is **0.4.1**. Reloading the extension invalidates the
old script already inside open tabs, so the article/X tab reload is required.

The enabled state survives page reloads, tab changes, and switching to another
app. No toolbar click is required. It turns off when the chatbot tab closes or
navigates away, when Chrome ends the browser session, or when the local receiver
becomes unavailable on the next delivery/30-second heartbeat. A blocked or
unsupported page clears stale text but leaves the green session state on. The
toolbar icon remains a harmless retry/republish control; it does not turn an
active session off.

The bridge publishes only the currently visible HTTP(S) tab, blocks login/password/payment contexts, never reads the chatbot page itself, limits text to 60,000 characters, and keeps a fresh page for five minutes. For an individual X status URL it returns only the primary post and excludes replies; image-only posts remain visual requests. It does not scroll or take screenshots. See the [extension notes](web_app/chrome_article_bridge/README.md).

Natural article wording routes to the bridge: “read/summarize/analyze the
article, news, webpage, or page on my screen.” Generic visual wording such as
“check my screen,” “look at this app/window,” or requests about a layout, image,
chart, appearance, or explicit screenshot instead use Desktop Control's
screenshot action when enabled. Ambiguous wording produces one short
metadata-only preflight: it checks for a fresh readable Chrome page and, when
Desktop Control is enabled, the frontmost app/window name. It does not return
page body text, screen labels, form values, or pixels. Clear context routes
automatically; unresolved or conflicting context produces one short clarifying
question. Article extraction never automatically falls back to a screenshot.
Reading a public page is read-only: the request itself is sufficient authorization,
so the chatbot does not ask for approval. If the bridge is not ready, it asks you
to reload the extension/page instead of offering a screenshot. Desktop actions and
screenshots remain limited to explicit control or visual requests.

## Optional tools

- **Coding agent** runs the locally installed `pi` executable from this repository's Node dependencies.
- **Desktop control** uses `~/.local/bin/desktop-harness` and requires macOS Accessibility permission for actions plus Screen Recording permission for screenshots. The server kill switch defaults on, but each browser starts with the tool off; enable it in **Tools → Desktop control**. It acts or captures only when explicitly requested. A screenshot can target the main display or a named visible app/window; sensitive sign-in/payment scopes remain blocked.
- **Memory** uses an editable personal Markdown profile and saved conversations. The assistant can update it when you say “remember…” or “forget…”.

## Development

```bash
uv run ruff check src tests
uv run mypy src
uv run pytest -q
node --check web_app/main.js
```

CI runs on macOS, checks the retained browser path, builds the Python package, and performs an installation smoke test. Publishing is handled by `.github/workflows/publish.yml` for `v*` tags.

### Contributing via pull requests

Work on a feature branch off `main`, open a pull request, and wait for review before merging. Keep each PR focused on one change so reviewers can follow the diff easily. After approval, merge into `main` and delete the branch.

## License

Apache-2.0. This project is derived from Hugging Face's original `chatbot` project; copyright notices are retained in [LICENSE](LICENSE).
