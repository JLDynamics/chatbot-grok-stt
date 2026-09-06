# Voice reliability work — session handoff

Updated September 5, 2026. Repository: `/Users/jack/Documents/chatbot`.

## Current objective and user expectations

The user wants the native voice assistant and its article-reading tools to work reliably as a complete system. They explicitly authorized fixing bugs and improving the workflow, and asked for real testing rather than repeatedly handing failures back to them. Explain results plainly. Preserve unrelated changes; do not commit, push, or merge without a request.

The latest reported failure was: the app stops responding. Investigation found the native panel displaying **“Can't reach the service”**, with neither backend running. The startup repair has now been verified through actual app cold startup, warm reuse, both partial-service recovery cases, cancellation, and quit ownership checks. Acoustic interruption and live desktop/coding behavior remain separate acceptance checks.

## Repository and environment state

- Branch: `codex/voice-workflow-reliability`; baseline commit: `1831b856d4a0`. Changes remain uncommitted.
- Pre-existing deletions: `CLAUDE.md` and `HANDOFF-fallback-chain.md`. Do not restore them. The original contributor-guide request produced the current `AGENTS.md`.
- Native app: `macos/Voice`; Python realtime service: `src/chatbot`; tools/memory/session sidecar: `web_app/server.py`.
- Default endpoints: `ws://127.0.0.1:8766/v1/realtime` and `http://127.0.0.1:7860/api`.
- Current launcher defaults use Parakeet recognition, a Responses API model, and Kokoro speech. Preserve configured models and voices; older memory about browser UI, Pi, or Qwen is stale.
- Credentials live in `~/.config/chatbot/env`; personal data lives in `~/.chatbot`. Do not print credentials or overwrite personal data.
- September 5 continuation: the app and services are being left ready for use, with the conversation ended and microphone off. Always inspect current listeners before another lifecycle test.
- `macos/Voice/build/Voice.app` was rebuilt successfully on September 5 after the startup cleanup fixes. The compiler required sandbox escalation for SwiftUI macro plugins.

## Startup repair — September 5 continuation

- `LocalServiceStarter` checks both standard local HTTP services before opening the microphone, launches `run-browser.sh --reuse-running` when needed, and keeps custom endpoints externally managed.
- Bash now receives a `PWD` matching its working directory. A first cold-start sample stalled in `getcwd`; a later sample stalled opening the script. Launching eventually proceeded and subsequent connection tests passed. macOS Documents permission was suspected but not confirmed; do not claim that cause was proven or that startup is instant.
- Startup logs now really live at `/tmp/voice-service-startup.log` (previously they were in macOS's per-user temporary folder despite error messages naming `/tmp`).
- A thrown startup error tears down the connection and clears its startup status before propagating the error.
- Services stay warm after End conversation or cancelled connection setup. Quitting Voice terminates its retained launcher, whose signal handler exits and cleans up only its own children. External services remain running.
- Launcher tests use fake services and temporary configuration, with no models, network, or personal data: cold, either service existing, both existing, occupied-port refusal, and termination cleanup. `CHATBOT_ENV` can be overridden for isolation.

Verified in the real app: cold start reached an active microphone and actual voice responses; warm reconnect resumed capture; End disabled the microphone; cancelling setup left the panel idle; either missing service was started while the existing service PID remained unchanged. Quit stopped the app-owned service and preserved the externally started service in both partial-service cases.

The direct live WebSocket tool test used correct `session.type = realtime`, fetched public IANA documentation through the sidecar, returned the tool output, and generated the follow-up audio: **one tool call, two completed responses, 677,888 audio bytes, 5.5 seconds, zero protocol errors**. This is backend/tool continuation evidence, not proof of acoustic interruption or desktop control.

Final checks: **654 Python tests passed**, one existing Starlette deprecation warning; Ruff lint/format passed for 107 files; native runtime checks passed; mypy passed for 72 Python source files (no Python source changes afterward); app build/sign succeeded. Changes remain uncommitted.

## Earlier implemented repairs

- `AudioEngine.swift`: speaker echo cancellation, mono input/output format matching, headphone/compatibility choices, visible fallback note, playback completion based on actual playback, stale-buffer generations.
- `VoiceRuntime.swift`: cancellable tool-work generations, playback tracker, and page-result metadata preservation.
- `LiveVoiceBackend.swift`: rejects stale sockets/tool results, cancels obsolete work, serializes response requests, tracks audible playback separately from server completion, and supports Stop reply.
- `VoiceSession.swift` / `TranscriptView.swift`: stable transcription item IDs update the same bubble; removed an overly broad word-overlap merge heuristic.
- UI: separate Stop reply, audio settings, correct next-conversation wording, typing in memory editor no longer triggers voice shortcuts.
- `PageReadWorkflow.swift`: X/Twitter and current-page requests prefer Chrome; ordinary URLs prefer fetch; optional browser preference for known login-dependent pages. Each method runs at most once. Partial text triggers the other method and remains available if neither is complete. Wrong-page results are rejected, while unavailable/expired bridge reasons are preserved.
- Screen fallback stays assistant-directed, screenshot/scroll only for ordinary reading, capped at six captures with identical-image detection. Explicit desktop tasks retain their tools. Do not claim this is a fully deterministic desktop reading pipeline.
- Sidecar: disconnect/timeout cancellation cleans up coding/desktop subprocess work; failed desktop results are not reported as verified success. An initial disconnect-watcher cancellation hang was caught and fixed with a stopped event.
- Seven Python source files have formatting-only changes; AST equivalence was checked before/after formatting. Preserve these unless deliberately revisiting them.

## Chrome extension: installed and live-tested

The user explicitly approved installing the repository's unpacked **Chatbot Page Bridge** into the current Chrome profile. It is installed from `web_app/chrome_article_bridge`, version 0.4.3. Do not ask again to reload this already-authorized extension after a fix.

Test article: `https://x.com/lingxi/status/2094493172516966781` — “Grok Bot for Engineering.”

Live extraction returned **12,328 characters**, with opening, final section, and closing present. The sidebar and reply composer were absent; metadata reported complete, not truncated, and comments excluded. The actual native tool executor read it via the bridge in one attempt. With X still open, a request for IANA documentation rejected the wrong Chrome page and successfully fetched the intended document.

The live test exposed two more extension bugs, now fixed in `background.js` and covered by executable Node-backed tests in `tests/test_web_app_server.py`:

- Switching from Chrome to the native app discarded the selected article. Native handoff now retains it for the existing bounded TTL; changing Chrome tabs still clears it.
- Restarting the MV3 background worker could end a native session because there was no browser receiver tab. Native session restoration now checks that the local service is alive.

The fixed extension was reloaded and enabled on the article. The rebuilt native app displayed “Chrome Extension Connected”; the actual native read still passed after switching to that app. Stopping the sidecar ends bridge sharing via its health check, so click the extension action again after starting services if needed. Installation remains intact.

## Verification evidence and limits

Before the newest startup repair:

- Full Python suite: **649 passed**, one Starlette deprecation warning.
- Ruff lint passed; formatting check passed for 106 files; earlier mypy check passed for 72 source files.
- Native executable regression checks passed; app built and signed.
- Actual MacBook mic/speaker test: matching audio formats resolved voice-processing startup failure. **56 mic frames during 5.55 seconds of speaker playback**, versus zero before the fix.
- Real app tests: playback drained back to listening, Stop reply cancelled while retaining the connection, end/reconnect resumed capture, memory-editor `m space m` typed without voice shortcuts. Temporary text was cleared without saving.
- Configured live search returned five results; live fetch returned 783 characters for `https://www.iana.org/help/example-domains`.
- Live extension/native reading tests described above passed.

During the latest unresponsive-app investigation, manually starting `./run-browser.sh` successfully loaded recognition, response model, and Kokoro. A direct real WebSocket text request completed in **1.7 seconds** and produced **202,752 audio bytes**. The temporary test's session update omitted `session.type = "realtime"`, causing one invalid-event error; the subsequent response still completed. That malformed test update is not evidence of a native-client bug: native `sendSessionUpdate()` includes the type. Use the correct session shape in subsequent tests.

Still unproven after the continuation: real acoustic interruption recognition/echo rejection when the user speaks over output; full live desktop/coding-model behavior. Continuous mic frames alone do not prove speech recognition or echo rejection quality.

## Practical continuation

1. Check current processes and the diff before changing anything. Preserve all unrelated modifications and deletions.
2. Startup and ownership checks above are complete. Revisit only for a reproduced failure; the first launch may be delayed by macOS file opening, whose precise cause is still unconfirmed.
3. Remaining acceptance: actual speech over assistant output (transcription, echo rejection, short stop, thinking pauses), live desktop/coding tasks, and app-visible handling of reproduced server failures. The client still ignores generic server error events; do not broaden that behavior without checking actual event semantics.
4. Keep configured models and personal data intact. Do not commit/push/merge without a user request.

Commands:

```bash
bash macos/Voice/scripts/test.sh
.venv/bin/pytest -q
.venv/bin/ruff check src/ tests/
.venv/bin/ruff format --check src/ tests/
.venv/bin/mypy src/
CLANG_MODULE_CACHE_PATH=/tmp/chatbot-swift-cache ./macos/Voice/scripts/build.sh
```

Logs: `/tmp/chatbot-server.log`, `/tmp/chatbot-web.log`, and the new `/tmp/voice-service-startup.log`. Native builds and live localhost/network tests have needed sandbox escalation; ordinary focused tests generally work directly. Avoid printing sensitive environment files or broad system logs.

UI automation used the `cua-driver` skill and `mcp__cua_repl`. Use the documented CUA APIs for UI actions; refresh accessibility state after actions. Some native SwiftUI clicks return an accessibility error despite succeeding, so verify fresh state. Do not reuse old element indices or tool process handles from this handoff.

Temporary live test sources may still exist at `/tmp/chatbot-live-native-reading.swift` and `/tmp/chatbot-live-reading-check.py`. The Swift test compiles actual repository executor sources, sends no article text to a conversation model, and prints only metadata. Inspect them before reuse; temporary files are not durable project tests.
