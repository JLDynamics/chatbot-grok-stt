# Chatbot

A Mac-first live voice chatbot that runs its ears and voice locally, while a Responses API model handles the conversation.

`native panel microphone → Parakeet MLX → Responses API → TTS (Kokoro-82M by default, or VibeVoice) → speakers`

It keeps realtime WebSocket turn-taking, interruption/cancellation, partial transcripts, long-term memory, in-response web search and page reading (including the Chrome page bridge for logged-in or paywalled tabs), in-app screenshots, and optional coding-agent delegation.

In the default **Speakers (echo cancellation)** mode, the mic stays open while the assistant speaks. The panel uses matched mono capture/render formats for Apple's voice processing, with Silero VAD detecting interruptions. If voice processing cannot start, the panel visibly reports compatibility mode, which suppresses microphone capture during playback. **Headphones** mode keeps capture open without echo cancellation. **Stop reply** stops playback and pending tools without ending the conversation. Saved conversations are replayed into the backend on connect (last 20 text turns). Personal memory is injected via instructions. VibeVoice runs locally without an AI watermark.

## Requirements

- Apple Silicon Mac
- Python 3.10 or newer
- [uv](https://docs.astral.sh/uv/)
- [Grok Build](https://x.ai/cli) (`grok` on `~/.local/bin`), for the optional coding agent
- An OpenRouter key

## Install and run

```bash
uv sync --group dev
./set-keys.sh
./run-browser.sh
```

```bash
./macos/Voice/scripts/build.sh
open macos/Voice/build/Voice.app
```

To show Voice in Finder → Applications, Launchpad, and Spotlight:

```bash
./macos/Voice/scripts/install.sh
```

`run-browser.sh` starts the realtime backend (`:8766`) and the API sidecar (`:7860`); the Voice panel connects to both. Clicking the installed app starts those services if they are not already running. The first launch may download the Parakeet and TTS model files.

The default model path is `openai/gpt-5.6-luna` through OpenRouter. **Parakeet TDT 1.1B** STT and the TTS model run locally with MLX. The default TTS is **Kokoro-82M** (voice `bm_fable`, auto-switching language/voice from the detected language); set `TTS=vibevoice` to use VibeVoice (`en-Emma_woman`).

## Configuration

The launch scripts read secrets from `~/.config/chatbot/env`, written with owner-only permissions by `set-keys.sh`. Environment values override saved values.

| Setting | Default | Purpose |
| --- | --- | --- |
| `TTS` | `kokoro` | TTS backend: `kokoro` (Kokoro-82M) or `vibevoice` (Microsoft VibeVoice) |
| `MODEL` | `openai/gpt-5.6-luna` | OpenRouter Responses API model ID |
| `KOKORO_MODEL` | `mlx-community/Kokoro-82M-bf16` | Kokoro-82M MLX model repo |
| `KOKORO_VOICE` | `bm_fable` | Kokoro voice (e.g. `bm_fable` British male, `af_heart` American female); auto-switches with the detected language |
| `KOKORO_LANG` | `b` | Kokoro language code (`a`/`b`/`e`/`j`/`f`/`i`/`p`/`z`/`h`) |
| `KOKORO_SPEED` | `1.0` | Kokoro speech-speed multiplier |
| `VIBEVOICE_MODEL` | `mlx-community/VibeVoice-Realtime-0.5B-8bit` | VibeVoice MLX model repo (alternative backend) |
| `VIBEVOICE_VOICE` | `en-Emma_woman` | VibeVoice voice from the repo (`*_voices/*.safetensors`) |
| `VIBEVOICE_MAX_TOKENS` | `1024` | Safety ceiling on generated tokens (model stops on EOS) |
| `VIBEVOICE_CFG_SCALE` | `1.5` | Classifier-free guidance; higher is more distinct but harsher |
| `KOKORO_DENOISE_FLOOR` / `VIBEVOICE_DENOISE_FLOOR` | `0.04` | Spectral-denoiser suppression floor; lower removes more hiss (slight risk of a processed texture) |
| `PORT` / `WEB_PORT` | `8766` / `7860` | Realtime and browser ports |
| `PARAKEET_MODEL` | `mlx-community/parakeet-tdt-1.1b` | MLX Parakeet STT model (English) |
| `VAD_MIN_SILENCE_MS` | `1200` | Silence (ms) before a spoken turn is considered finished. Higher keeps ~1 s thinking pauses inside one turn instead of splitting it; lower answers faster after you truly stop |
| `VAD_THRESH` | `0.60` | VAD confidence threshold; higher = fewer false voice triggers |
| `VAD_MIN_SPEECH_MS` | `600` | Sustained speech (ms) before a user turn / barge-in is confirmed. Raise to soften barge-in (so brief noises or the assistant's own echo don't cut a reply) |
| `PROMPT` | concise voice prompt | Backend system prompt |
| `STARTUP_GREETING` | empty | Optional greeting instruction on connection |
| `TINYFISH_API_KEY` / `TAVILY_API_KEY` / `SERPER_API_KEY` | empty | Enables local web search; TinyFish also powers page fetch |
| `CODE_AGENT` | `on` | Set `off` to hide coding-agent delegation |
| `CODE_AGENT_CWD` | home folder | Default working folder for coding tasks |
| `CODE_AGENT_MODEL` | `grok-4.6` | Model for coding-agent delegation (via Grok Build) |
| `DESKTOP_CONTROL` | `on` | Unused by Voice; kept for the sidecar verify harness |
| `CHATBOT_DATA_DIR` | `~/.chatbot` | Saved chats, personal profile, and project notebooks |
| `CHATBOT_SESSION_RETENTION` | `50` | Maximum saved conversations kept on disk |

### Search, fetch, and the Chrome bridge

The model searches and reads pages **inside the same reply** — it does not wait for the Mac app to round-trip each hop.

- **Local web search**: TinyFish (preferred), Tavily, or Serper. Use when a fact may be outdated or the model is not sure.
- **`read_page`**: one tool that tries public fetch, then the live Chrome tab when fetch is gated, login-only, or the page is already on screen. X/Twitter links start with Chrome. TinyFish fetch falls back to a direct HTTP read if the provider fails.
- **Chrome page bridge**: bounded reader-style text from the visible public tab, including pages a normal fetch cannot open. Read-only; no replies/timelines on X. The extension still has to be enabled on the tab.
- **Screenshot** stays in Voice.app (Screen Recording is per app). It is for visual questions, not for reading articles.

## Chrome page bridge

The extension is part of the live product at [`web_app/chrome_article_bridge`](web_app/chrome_article_bridge). There is no chatbot browser tab; the bridge pairs with the native Voice panel.

1. Keep the sidecar running (`./run-browser.sh`, port `7860`).
2. Open `chrome://extensions`.
3. Enable **Developer mode**.
4. Choose **Load unpacked** and select `web_app/chrome_article_bridge`.
5. Open the public page you want read, then click the bridge toolbar icon once to enable it.

If an older unpacked copy still points to the deleted `demo/chrome_article_bridge`
folder, remove it and load the `web_app` path above. When bridge code changes,
use the extension card's **Reload** button and then click the bridge toolbar
icon once on the article tab; the click re-injects the new content script, so
no page reload is needed.
The current card version is **0.4.3**. A stored session from before the browser-UI
removal disables itself once; re-enable it with one toolbar click and it persists
in native mode from then on.

The enabled state survives page reloads and tab changes. It turns off when
the local sidecar becomes unavailable on the next 30-second heartbeat. A blocked or
unsupported page clears stale text but leaves the green session state on. The
toolbar icon is the enable control; it does not turn an active session off.
The bridge publishes only the currently visible HTTP(S) tab, blocks login/password/payment contexts, never reads the chatbot page itself, limits text to 60,000 characters, and keeps a fresh page for five minutes. For an individual X status URL it returns only the primary post and excludes replies; image-only posts remain visual requests. It does not scroll or take screenshots. See the [extension notes](web_app/chrome_article_bridge/README.md).

Natural article wording uses `read_page` (fetch, then Chrome when needed). Generic visual wording such as “check my screen,” “look at this app/window,” or an explicit screenshot uses the in-app screenshot tool. Article extraction never automatically falls back to a screenshot. Reading a public page is read-only: the request itself is sufficient authorization. If the bridge is not ready, the assistant says so and can still try a public fetch when it has a URL.

## Optional tools

- **Coding agent** runs the locally installed [Grok Build](https://x.ai/cli) `grok` CLI (model `grok-4.6`) on this Mac when you ask it to inspect or change files.
- **Screenshot** captures the main display from Voice.app. Screen Recording permission is required. Sensitive sign-in/payment windows stay blocked.
- **Memory** uses an editable personal Markdown profile and saved conversations. The assistant can update it when you say “remember…” or “forget…”.

## Development

```bash
uv run ruff check src tests
uv run mypy src
uv run pytest -q
bash macos/Voice/scripts/test.sh
```

CI runs on macOS (lint, types, tests), builds the Python package, and performs an installation smoke test. Publishing is handled by `.github/workflows/publish.yml` for `v*` tags.

### Contributing via pull requests

Work on a feature branch off `main`, open a pull request, and wait for review before merging. Keep each PR focused on one change so reviewers can follow the diff easily. After approval, merge into `main` and delete the branch.

The native test runner covers playback queue generations, cancelled work, page fallback limits, and transcript revisions. See [voice reliability validation](docs/voice-reliability-validation.md) for hardware evidence and the remaining live checks.

## License

Apache-2.0. This project is a fork of Hugging Face's [`speech-to-speech`](https://github.com/huggingface/speech-to-speech) project. The upstream copyright notice is retained in [LICENSE](LICENSE) and [NOTICE](NOTICE), with the fork's copyright (Jack) added alongside.
