# Chatbot

A Mac-first live voice chatbot that runs its ears and voice locally, while a Responses API model handles the conversation.

`native panel microphone → on-device macOS speech-to-text → Responses API → Siri text-to-speech → speakers`

It keeps realtime WebSocket turn-taking, interruption/cancellation, long-term memory, in-response web search and page reading (including the Chrome page bridge for logged-in or paywalled tabs), in-app screenshots, and optional coding-agent delegation.

In the default **Speakers (echo cancellation)** mode, the mic stays open while the assistant speaks. The panel uses matched mono capture/render formats for Apple's voice processing, with Silero VAD detecting interruptions. If voice processing cannot start, the panel visibly reports compatibility mode, which suppresses microphone capture during playback. **Headphones** mode keeps capture open without echo cancellation. **Stop reply** stops playback and pending tools without ending the conversation. Saved conversations are replayed into the backend on connect (last 20 text turns). Personal memory is injected via instructions. VibeVoice runs locally without an AI watermark.

## Requirements

- Apple Silicon Mac, macOS 26 or newer (the on-device speech engine)
- Python 3.10 or newer
- [uv](https://docs.astral.sh/uv/)
- An OpenRouter key

## Install and run

```bash
uv sync
./set-keys.sh
./run-browser.sh
```

`uv sync` installs the app and its dependencies.

```bash
./macos/Voice/scripts/install.sh
open /Applications/Voice.app
```

Use the copy in **Applications**. The tree under `macos/Voice/build/` is only the compiler output; Launchpad should not list it. `run-browser.sh` starts the realtime backend (`:8766`) and the API sidecar (`:7860`). Opening Voice starts those services when the default local ports are used, and **replaces a stale process** whose code no longer matches this checkout (that is how an old backend could sit on the ports without search). The first launch may download the TTS model files.

The default model path is `openai/gpt-5.6-luna` through OpenRouter. Speech-to-text runs **on this Mac** through Apple's on-device engine, so there is no key, no metering, and nothing that expires: a turn transcribes punctuated in ~170ms, including the helper process launch, and a live 33s turn with four pauses took 370ms. It covers 45 locales; `STT_LOCALE` picks one, because the engine has no auto-detect. `macos/SpeechHelper/build/speech-helper --locales` lists what this Mac supports and which models are already installed, and a locale's model downloads itself the first time the backend starts with it. It transcribes fillers literally and does not know proper nouns it has no context for, which a hosted service tidied.

Text-to-speech uses **Apple's Siri voices** (`en-US-F` by default) through
[siri-tts](https://github.com/maximilianromer/siri-tts-cli), which reaches the neural voices Apple
publishes to no API: `AVSpeechSynthesisVoice` never lists them and `say -v` ignores their names.
Measured against the Kokoro backend it replaced, on the same sentence, it is 84ms to first audio
versus 208ms. That binary dlopens private frameworks, so a macOS update can break it; `TTS=vibevoice`
is the fallback, running entirely on MLX with nothing private underneath. VibeVoice is far slower
(roughly real time), so it is a way to keep talking after a break, not a daily driver.

`TTS=vibevoice` runs **VibeVoice** (`en-Emma_woman`) locally with MLX.

This build does not speak Chinese. No backend here has a Chinese voice, so a Han character in a reply is dropped rather than read aloud — espeak-ng, which the English front end falls back to, pronounces one as the words “Chinese letter.”

## Configuration

The launch scripts read secrets from `~/.config/chatbot/env`, written with owner-only permissions by `set-keys.sh`. Environment values override saved values.

| Setting | Default | Purpose |
| --- | --- | --- |
| `STT_LOCALE` | `en-US` | Transcription locale, e.g. `en-GB`, `en-AU`, `ja-JP`, `fr-FR`. The engine has no auto-detect, so this fixes the spoken language |
| `TTS` | `siri` | TTS backend: `siri` (Apple's Siri voices) or `vibevoice` (Microsoft VibeVoice, the local fallback) |
| `SIRI_VOICE` | `en-US-F` | Siri voice name; `siri-tts voices --available` lists what this Mac has installed |
| `SIRI_TTS_BIN` | `~/.local/bin/siri-tts` | Path to the siri-tts binary |
| `MODEL` | `openai/gpt-5.6-luna` | OpenRouter Responses API model ID |
| `VIBEVOICE_MODEL` | `mlx-community/VibeVoice-Realtime-0.5B-8bit` | VibeVoice MLX model repo (alternative backend) |
| `VIBEVOICE_VOICE` | `en-Emma_woman` | VibeVoice voice from the repo (`*_voices/*.safetensors`) |
| `VIBEVOICE_MAX_TOKENS` | `1024` | Safety ceiling on generated tokens (model stops on EOS) |
| `VIBEVOICE_CFG_SCALE` | `1.5` | Classifier-free guidance; higher is more distinct but harsher |
| `VIBEVOICE_DENOISE_FLOOR` | `0.04` | Spectral-denoiser suppression floor; lower removes more hiss (slight risk of a processed texture) |
| `PORT` / `WEB_PORT` | `8766` / `7860` | Realtime and browser ports |
| `VAD_MIN_SILENCE_MS` | `1200` | Silence (ms) before a spoken turn is considered finished. Higher keeps ~1 s thinking pauses inside one turn instead of splitting it; lower answers faster after you truly stop |
| `VAD_THRESH` | `0.65` | VAD confidence threshold; higher = fewer false voice triggers |
| `VAD_MIN_SPEECH_MS` | `600` | Sustained speech (ms) before a user turn / barge-in is confirmed. Raise to soften barge-in (so brief noises or the assistant's own echo don't cut a reply) |
| `PROMPT` | concise voice prompt | Backend system prompt |
| `STARTUP_GREETING` | empty | Optional greeting instruction on connection |
| `TINYFISH_API_KEY` / `TAVILY_API_KEY` / `SERPER_API_KEY` | empty | Enables local web search; TinyFish also powers page fetch |
| `DESKTOP_CONTROL` | `on` | Unused by Voice; kept for the sidecar verify harness |
| `CHATBOT_DATA_DIR` | `~/.chatbot` | Saved chats, personal profile, and project notebooks |
| `CHATBOT_SESSION_RETENTION` | `50` | Maximum saved conversations kept on disk |

### Search, fetch, and the Chrome bridge

The **voice model** searches and reads pages **inside the same reply**. It answers from knowledge when that knowledge is still current. If a fact may have changed since training (who holds office, versions, scores, news, prices), it says a short line such as “Let me check that” and runs `bash` with `curl` on the server — without waiting for the user to ask. No Mac round trip per hop.

- **Research (`bash` + `curl`)**: the conversational model writes a short curl command, like Pi. No search API key. For latest news it should use a dated RSS feed (`when:1d`).
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

- **Screenshot** captures the main display from Voice.app. Screen Recording permission is required. Sensitive sign-in/payment windows stay blocked.
- **Memory** uses an editable personal Markdown profile and saved conversations. The assistant can update it when you say “remember…” or “forget…”.

## Development

```bash
uv run ruff check src tests
uv run mypy src
uv run pytest -q
bash macos/Voice/scripts/test.sh
python3 scripts/verify-voice.py --skip-ui --research   # live search/read_page, services must be up
```

CI runs on macOS (lint, types, tests), builds the Python package, and performs an installation smoke test. Publishing is handled by `.github/workflows/publish.yml` for `v*` tags.

### Contributing via pull requests

Work on a feature branch off `main`, open a pull request, and wait for review before merging. Keep each PR focused on one change so reviewers can follow the diff easily. After approval, merge into `main` and delete the branch.

The native test runner covers playback queue generations, cancelled work, page fallback limits, and transcript revisions. See [voice reliability validation](docs/voice-reliability-validation.md) for hardware evidence and the remaining live checks.

## License

Apache-2.0. This project is a fork of Hugging Face's [`speech-to-speech`](https://github.com/huggingface/speech-to-speech) project. The upstream copyright notice is retained in [LICENSE](LICENSE) and [NOTICE](NOTICE), with the fork's copyright (Jack) added alongside.
