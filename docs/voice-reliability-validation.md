# Voice reliability validation

## Changes

- Speaker mode enables Apple voice processing with matching mono input and output formats. The original stereo-output/mono-input mismatch reproduced error `-10875` on this MacBook; explicitly connecting the main mixer to the output with the same format resolved it.
- Microphone frames continue during playback when voice processing works. Compatibility mode remains available and visibly explains its interruption limitation. Headphones mode does not suppress capture.
- Playback completion follows `dataPlayedBack`; callbacks from abandoned queues cannot drain a new reply. Stop reply cancels work while retaining the conversation.
- Connection and tool generations reject obsolete socket events and tool results. Cancelled tools receive cancellation results, and the sidecar monitors disconnected clients to terminate pending coding/desktop work.
- Stable transcription item IDs update existing bubbles, including interim revisions. Distinct utterances no longer merge merely because they share several words.
- `read_page` tries each enabled text method at most once, retains failures and completeness, and rejects a different Chrome page. X/Twitter links and current-page requests prefer Chrome; other URLs prefer fetch, with a browser-first option for known login-dependent pages. Partial results trigger the other text method and remain available if neither provides a complete page. Screen reading is limited to six captures and stops on identical captures. Screenshot comparison is byte-based; changing page animations can evade duplicate detection, but the six-capture cap still applies.
- Desktop results distinguish execution from verified screen changes. Failed or malformed harness output is an error. Ordinary typing in editable fields no longer activates voice shortcuts.

## Automated checks

```bash
uv run pytest -q
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
uv run mypy src/
bash macos/Voice/scripts/test.sh
./macos/Voice/scripts/build.sh
```

The full Python suite passed **649 tests**. Ruff passed; mypy reported no issues in 72 source files. The native executable tests passed playback, cancellation, fallback, disabled-tool, screen-limit, and transcript-revision cases. The native app compiled and signed successfully.

Tests use controlled responses and temporary data. The fallback suite now stubs DNS so its unit tests do not depend on public DNS availability.

## MacBook hardware and interface checks

Used the built-in microphone and speakers at 48 kHz, the actual native app, and temporary loopback services with synthetic conversation content and in-memory storage. No cloud conversation model was used for these checks.

- Reproduced the voice-processing startup failure before matching the formats.
- Confirmed successful startup afterward, without compatibility-mode fallback.
- During a 5.55-second speaker sentence, received **56 microphone frames**; the failed configuration received **zero** during playback.
- Verified that playback draining returns the panel to listening.
- Clicked Stop reply with queued playback: the fixture received cancellation, the control disappeared, and the connection stayed open.
- Ended and restarted the conversation: a fresh connection opened and microphone frames resumed.
- Inspected the audio picker and its three modes in the running interface.
- Typed `m`, space, and `m` in the final app's memory editor: all three characters appeared without starting voice or toggling the microphone. Cleared the temporary text without saving.

## Remaining acceptance checks

Live configured-provider checks also passed: search returned five results for public IANA documentation, and fetch returned 783 characters from its example-domains page with neither gating nor truncation reported. These requests exercised the sidecar's actual provider clients; they did not use mocked provider responses. The updated native routing tests and 49 focused sidecar tests passed afterward.

Continuous PCM transport proves the microphone path remains open; it does not prove speech recognition quality or acoustic echo rejection. With the real backend, speak over an assistant reply using the MacBook speakers. Confirm that the reply stops, the new words are transcribed, and the assistant does not answer its own voice. Repeat with a thinking pause and a short “stop.”

Desktop interactions across third-party apps and the coding model were not exercised by the synthetic hardware fixture. Their automated contracts and failure handling passed, but live behavior needs separate coverage.

## Live X bridge validation

Installed the repository's Chatbot Page Bridge in the current Chrome profile with user approval. The live extension returned 12,328 characters for “Grok Bot for Engineering,” with the opening, final section, and closing text present. The sidebar and reply composer were absent; extraction reported the X article-body boundary, excluded comments, and no truncation.

The native client's actual tool executor then read that article using only the bridge. A second request deliberately asked for IANA documentation while X remained open: the client rejected the mismatched Chrome page and fetched the correct public document.

This live test uncovered and fixed two native-session lifecycle gaps: switching away from Chrome removed the selected article, and a restarted extension worker could end a session without a browser receiver tab. Native handoff now retains the selected page for the existing bounded TTL; actual Chrome tab switches still clear it. Worker restart restores a native session only while the local service is available. Executable extension tests cover handoff, tab changes, worker restart, and service failure.

Also fixed unavailable-bridge failures being mislabeled as a different page. Missing/expired bridge reasons now survive the fallback, with a native regression check.

After rebuilding, opened the actual native app and its settings, confirmed “Chrome Extension Connected,” and reran the native read successfully while the app had focus. The full 649-test suite, native runtime checks, lint, formatting, and signed build passed after these fixes. Stopped the temporary sidecar after testing; use the normal launcher for a voice session, then click the bridge icon to re-enable sharing if the service-stop check has ended its session.


## September 5 startup and lifecycle continuation

The app was tested with both services stopped, with both running, and with either service missing. Cold startup reached microphone capture and live model replies. Warm reuse opened another connection. Both missing-service cases recovered without replacing the externally started process.

Cancel during startup and End conversation returned the panel to idle with the microphone disabled. Services remain warm between conversations. Quit now terminates the app-owned launcher; live checks in both partial-service cases showed that its child stopped while the external service survived. Five isolated launcher tests cover startup combinations, occupied-port refusal, and signal cleanup.

Fixed startup failure cleanup, the incorrect startup log location, and Bash working-directory inheritance. A first-launch delay was observed in macOS file opening; a Documents permission issue was suspected but not established. Do not treat this as proof of consistently fast startup.

A real WebSocket request, using the correct realtime session shape, called the sidecar to fetch IANA example-domain documentation, supplied the tool result, and completed a follow-up spoken response. Result: one tool call, two completed responses, 677,888 audio bytes in 5.5 seconds, zero protocol errors. This test did not record microphone audio or alter saved sessions.

After these changes: 654 Python tests passed (one existing Starlette warning), native runtime checks passed, Ruff lint and formatting passed (107 files), and the app compiled and signed. Mypy passed on 72 Python source files; the continuation did not change those source files. Acoustic interruption and live desktop/coding acceptance remain outstanding.
