# Local Voice Backend for Official Codex CLI

Canonical plan path:
`/Users/jack/Documents/chatbot-grok-stt-isolated/VOICE-SWAP-HANDOFF.md`

Every agent must read this file from that absolute path before editing either
repository. Keep this file as the single source of truth; do not make a second,
independently edited copy in the Codex checkout.

## Read this first

This document replaces the earlier voice-swap plan. The earlier plan treated the
official Codex voice session as a simple audio pipe and proposed translating a few
event names in this chatbot repository. That architecture was incorrect.

The official Codex voice session has two distinct parts:

1. **GPT-Live is the conversational voice front end.** It listens, transcribes,
   speaks, manages interruptions, and decides when to hand work to Codex.
2. **The selected Codex model is the agentic back end.** It reasons, edits files,
   runs tools, and returns results to the voice front end.

The requested change is a real replacement of part 1. Codex must remain the
agentic brain, while Jack's local macOS stack becomes the listener and speaker.

That cannot be implemented only by renaming WebSocket events in
`chatbot-grok-stt`. It requires a custom source build of the official OpenAI Codex
CLI plus a small local voice adapter extracted from this chatbot.

## Exact objective

Build a custom Codex CLI that keeps all of the following official Codex behavior:

- the selected Codex model and reasoning;
- the current thread, history, tools, permissions, approvals, and TUI;
- the existing realtime transcript and handoff flow where practical;
- the existing cloud voice mode as a fallback.

Add a second voice backend named `local` that uses Jack's local components:

- macOS microphone capture and speaker playback;
- echo cancellation;
- Silero VAD and barge-in detection;
- Apple's on-device SpeechHelper for speech-to-text;
- `siri-tts` for spoken output.

The local voice backend must contain no chatbot LLM, memory, search, screenshot,
browser, or tool logic. Codex remains responsible for all intelligence and work.

The deliverable is a separate custom executable such as `codex-local-voice`.
Never overwrite `/opt/homebrew/bin/codex`.

## Target and non-target

**Target:** the open-source official Codex CLI/TUI, based on the exact installed
version `codex-cli 0.155.0` and official tag `rust-v0.155.0`.

**Not targeted:**

- the closed ChatGPT desktop application UI;
- OpenCodex as an implementation base;
- a fake GPT-Live or Frameless server;
- modification of Jack's installed Homebrew Codex binary;
- deletion of the existing chatbot before the replacement works.

OpenCodex may be consulted as a secondary reference, but implementation decisions
must come from the official `openai/codex` source and official OpenAI docs.

## Why there are two workspaces

This job crosses two independent codebases. One worktree cannot safely represent
both.

| Location | Purpose | Write policy |
|---|---|---|
| `/Users/jack/Documents/chatbot-grok-stt` | Jack's working chatbot on `main` | Never modify |
| `/Users/jack/Documents/chatbot-grok-stt-isolated` | Extract and test the local voice adapter | Edit here |
| `/Users/jack/Documents/codex-local-voice` | Custom build of official `openai/codex` | Create, then edit here |
| `/Users/jack/Documents/opencodex` | Reverse-engineered proxy/reference | Read-only; not part of the implementation |
| `/opt/homebrew/bin/codex` | Installed official Codex 0.155.0 | Never modify or replace |

The existing chatbot worktree protects Jack's main chatbot. It is not where Codex
source changes belong. Codex changes belong in the separate official-source
checkout.

## Required Codex checkout

Before implementation, create the Codex source checkout once:

```bash
git clone --branch rust-v0.155.0 https://github.com/openai/codex.git \
  /Users/jack/Documents/codex-local-voice
cd /Users/jack/Documents/codex-local-voice
git switch -c feature/local-voice-backend
```

Expected official source commit for the installed 0.155.0 tag:
`f0a1b8f0849d90960bc406b848f32e5a129b0457`.

Do not copy official Codex source into the chatbot repository. If more than one
agent edits official Codex, create additional Git worktrees from
`/Users/jack/Documents/codex-local-voice` after the first checkout exists.

## Correct final data flow

```text
microphone
  -> local audio/VAD engine
  -> on-device SpeechHelper
  -> final user transcript
  -> Codex local voice backend
  -> existing Codex handoff/turn routing
  -> selected Codex model and tools
  -> final speakable Codex text
  -> Codex local voice backend
  -> siri-tts
  -> speaker
```

For barge-in:

```text
user starts speaking while Siri is talking
  -> local VAD emits speech_started
  -> local playback stops immediately
  -> the next final transcript steers or starts the Codex turn
```

No audio is sent to GPT-Live in local mode. Normal Codex Responses traffic still
goes to the selected Codex model.

## Reuse the official Codex realtime contract

Do not invent a Frameless event-renaming layer. The official Codex source already
has the application-level contract needed by the TUI:

- `thread/realtime/start` starts a thread-scoped voice session;
- transcript notifications update the visible caption/history;
- `RealtimeEvent::HandoffRequested` routes spoken input to the selected Codex
  thread;
- Codex final output is sent through `thread/realtime/appendSpeech`;
- `thread/realtime/stop` ends the voice session.

The cloud implementation currently converts GPT-Live events into this contract.
The new local implementation should produce and consume the same internal events.
This keeps the TUI, thread routing, response delivery, and replay behavior intact.

Useful official Codex 0.155.0 source files:

- `codex-rs/core/src/realtime_conversation.rs`
- `codex-rs/protocol/src/protocol.rs`
- `codex-rs/app-server-protocol/src/protocol/v2/realtime.rs`
- `codex-rs/app-server/src/request_processors/turn_processor.rs`
- `codex-rs/tui/src/app_server_session/realtime.rs`
- `codex-rs/tui/src/app/thread_routing.rs`
- `codex-rs/tui/src/chatwidget/realtime.rs`
- `codex-rs/prompts/templates/realtime/`

Prefer reusing `RealtimeEvent`, transcript notifications, handoff routing, and
`appendSpeech`. Add new protocol types only if the existing types cannot express
a required local state.

## Backend selection

Extend the official Codex realtime configuration rather than redirecting Live
URLs.

The existing `RealtimeTransport` enum supports `webrtc` and `websocket`. Add a
third value, `local`, and add machine-local configuration for the local voice
adapter command or socket.

Proposed development configuration in Jack's user-level
`~/.codex/config.toml` only:

```toml
[realtime]
transport = "local"
local_command = [
  "/Users/jack/Documents/chatbot-grok-stt-isolated/.venv/bin/python",
  "-m",
  "chatbot.local_voice_adapter",
]
```

Milestone 2 must make that module runnable from any current working directory,
for example by installing this worktree into its own virtual environment. Do not
depend on launching Codex from the chatbot directory.

Security requirement: a repository's `.codex/config.toml` must not be allowed to
select an arbitrary local command. `local_command` must come from user-level
configuration, a CLI override, or a trusted packaged path. Add config-loader tests
for this rule.

Do not use `experimental_realtime_ws_base_url` for local mode. That setting only
redirects the realtime WebSocket/sideband. WebRTC call creation has a separate
URL, and implementing both URLs would still require a complete Live server. A
project-local `.codex/config.toml` cannot override this base URL anyway.

## Local voice adapter contract

The Codex process and local voice adapter should exchange small text/control
messages. Raw microphone and speaker audio should stay inside the local adapter.

Use a versioned JSONL or length-prefixed JSON contract over inherited stdin/stdout
or a loopback Unix socket. Pick one transport and test it; do not support several
in the first implementation.

Minimum Codex-to-adapter messages:

| Type | Required fields | Meaning |
|---|---|---|
| `start` | `protocol`, `session_id` | Start devices and listening |
| `speak` | `response_id`, `text` | Render Codex text with Siri TTS |
| `stop_speaking` | `response_id` | Cancel current TTS/playback |
| `set_muted` | `muted` | Mute or unmute microphone capture |
| `shutdown` | none | Close devices and exit cleanly |

Minimum adapter-to-Codex messages:

| Type | Required fields | Meaning |
|---|---|---|
| `ready` | `protocol` | Adapter and devices are ready |
| `speech_started` | `utterance_id` | User began a real utterance; stop playback |
| `transcript_final` | `utterance_id`, `text` | Final local STT text to hand to Codex |
| `speech_stopped` | `utterance_id` | User turn ended |
| `playback_started` | `response_id` | Siri playback began |
| `playback_done` | `response_id` | Siri playback drained or was cancelled |
| `error` | `code`, `message`, `recoverable` | Structured failure |
| `closed` | none | Adapter shut down cleanly |

Rules:

- Reject unsupported protocol versions before opening devices.
- Every utterance and response must have an ID so stale output can be discarded.
- `speech_started` must stop local playback immediately; it must not wait for STT.
- An empty transcript must not start a Codex turn.
- A stale `playback_done` must not mark a newer response complete.
- Never write logs to the same stream used for framed protocol messages.

## What to extract from the chatbot

Work only in `/Users/jack/Documents/chatbot-grok-stt-isolated`.

Reuse behavior from:

- `macos/Voice/Sources/Audio/AudioEngine.swift` for microphone, speaker, and
  voice processing;
- `src/chatbot/VAD/` for speech start/stop and barge-in decisions;
- `src/chatbot/STT/native_stt_handler.py` and `macos/SpeechHelper/` for local STT;
- `src/chatbot/TTS/siri_tts_handler.py` for streaming Siri TTS;
- `src/chatbot/pipeline/cancel_scope.py` for stale-generation cancellation.

Do not import or call:

- `src/chatbot/LLM/` model, tool, research, or memory code;
- `web_app/` search, page-reading, session, or browser code;
- screenshot tools;
- saved chat or personal memory code.

Do not delete those files during extraction. First create a new, isolated adapter
entry point and prove it works. Removing old product code is a separate cleanup
decision after the Codex integration is complete.

The current native audio engine lives inside Voice.app. During the first real
audio milestone, it is acceptable to keep Voice.app running as an audio host even
if its window is temporarily hidden or still visible. Headless packaging comes
after the end-to-end path works. Do not combine UI removal with the first
integration attempt.

## Prompt and speaking style

The local adapter is not an LLM, so prompts do not belong in the chatbot adapter.
Speaking style belongs on the Codex side.

The official cloud voice front end currently has its own prompt at
`codex-rs/prompts/templates/realtime/backend_prompt.md`. Removing GPT-Live removes
that intermediary prompt. Local mode therefore needs a dedicated Codex developer
instruction inserted only while local voice is active.

Use the approved Codex personality text exactly:

```text
As Codex, you are a curious, thoughtful collaborator and a lucid communicator. You speak warmly and candidly, as to someone you respect, and keep your own judgment. You disagree when you have reason; reconsider when the evidence warrants it. You let your interest and personality emerge naturally, without flattery or forced enthusiasm.
```

Use these exact style paragraphs from the installed Codex CLI:

```text
Your writing adapts to the conversation, matching the tone and understanding of the user. Make sure to state the main point clearly and early, then develop it with the explanation and detail the reader needs. Let each sentence build on what came before. Develop the points that matter and provide enough support to be useful.

Use plain, simple language: familiar words, concrete examples, and precise verbs. Prefer active voice and direct statements. Write in connected prose. Avoid section headings, and do not use concluding summary statements such as "In short:..", "The simplest mental model is:...".
```

Then add this clearly labeled local-voice supplement:

```text
The user is speaking directly to you. Your response will be read aloud by text-to-speech. Use ordinary spoken English, with no Markdown, headings, bullets, emoji, code fences, URLs, or stage directions unless the user explicitly asks for text that requires them. Treat transcripts as imperfect: follow the likely meaning when it is clear, and ask one short clarification only when the ambiguity changes the action. Keep simple answers brief. For completed work, say what changed, how it was checked, and what remains unresolved.
```

Before injecting the personality block, test whether the normal Codex base prompt
already contains it. It must appear once, not twice. The voice supplement should
be local-mode-only and must not alter typed Codex sessions.

## Implementation milestones

Complete these milestones in order. Do not start by stripping the chatbot or
rewriting the TUI.

### Milestone 0 — Establish clean checkouts

1. Confirm `/Users/jack/Documents/chatbot-grok-stt-isolated` is on
   `feature/isolated-work`.
2. Confirm `/Users/jack/Documents/chatbot-grok-stt` remains clean on `main`.
3. Create `/Users/jack/Documents/codex-local-voice` from official tag
   `rust-v0.155.0` and branch `feature/local-voice-backend`.
4. Read each repository's `AGENTS.md` before editing.

### Milestone 1 — Lock the adapter contract with a fake engine

1. Add `local` to the official Codex realtime transport configuration.
2. Implement a fake local adapter that reads commands and emits deterministic
   transcript/playback events without microphone hardware.
3. Add the Codex-side process/IPC client with strict lifecycle and ID checks.
4. Reuse the existing app-server realtime notifications and TUI paths.
5. Prove: fake transcript -> existing Codex handoff -> selected Codex turn ->
   `appendSpeech` -> fake adapter.

Stop here if this cannot be proven. Do not begin native audio extraction until the
Codex handoff loop works with the fake adapter.

### Milestone 2 — Build the chatbot voice-only adapter

1. Add a new adapter entry point in the isolated chatbot worktree.
2. Keep only audio, VAD, STT, TTS, cancellation, and IPC in its dependency graph.
3. Make LLM/model/tool imports impossible from that entry point; test this.
4. Implement the versioned message contract above.
5. Test with prerecorded PCM and a fake TTS sink before opening real devices.

### Milestone 3 — Connect real Codex to the local adapter

1. Replace the fake adapter command with the real local adapter.
2. Send `transcript_final` into the same internal handoff route used by cloud
   realtime sessions.
3. Send accepted Codex speech text to `siri-tts` through `speak`.
4. On `speech_started`, stop Siri playback and discard stale response IDs.
5. Keep cloud `webrtc` and `websocket` modes unchanged.

### Milestone 4 — Native audio and headless cleanup

1. Verify microphone permission, voice processing, and speaker playback on
   macOS 27.
2. Once end-to-end behavior is proven, move the native audio host into a helper
   with no visible conversation UI, or hide the existing Voice.app window.
3. Preserve a clear status/error path for microphone denial, missing
   SpeechHelper, and missing `siri-tts`.
4. Do not remove the original chatbot UI/product code as part of this milestone.

### Milestone 5 — Package safely

1. Build a separate executable named `codex-local-voice`.
2. Keep `/opt/homebrew/bin/codex` unchanged and usable.
3. Use user-level config or a dedicated profile for local voice.
4. Document one command to launch the custom build and one command to return to
   official Codex.

## Required automated tests

### Official Codex checkout

- `realtime.transport = "local"` parses; existing `webrtc` and `websocket` still
  parse and behave unchanged.
- Project config cannot inject or replace `local_command`.
- Adapter protocol rejects wrong versions and malformed messages.
- Adapter exit, timeout, broken pipe, and duplicate/stale IDs fail cleanly.
- Fake final transcript creates exactly one Codex handoff/turn.
- Empty transcript creates no turn.
- Codex final text creates exactly one `speak` command.
- Barge-in creates `stop_speaking` before accepting the next transcript.
- Stale playback completion cannot complete a newer response.
- Local mode makes no GPT-Live/WebRTC/Frameless network request.
- Cloud realtime tests remain green.
- Local voice instructions are injected once and typed sessions are unchanged.

### Chatbot isolated worktree

- prerecorded PCM -> VAD -> one final transcript;
- short noise does not create a transcript;
- `speak` streams Siri audio and reports `playback_done`;
- `stop_speaking` terminates playback promptly;
- second utterance cancels stale output from the first;
- adapter entry point imports no LLM, search, memory, screenshot, or web modules;
- shutdown closes helper processes and devices.

## Required real-device checks

Automated tests do not prove actual microphone or speaker behavior. Report these
checks separately:

1. Start `codex-local-voice` on a real Codex thread.
2. Speak naturally for at least 30 seconds with a pause in the middle.
3. Confirm one correct transcript reaches that same thread.
4. Confirm the selected Codex model performs a simple repository task.
5. Confirm Siri speaks the concise result.
6. Interrupt Siri once; playback must stop and the new utterance must steer or
   start the next Codex turn.
7. Continue for two minutes without duplicate turns, stale speech, or a crash.
8. Confirm logs show no GPT-Live/Frameless voice connection in local mode.
9. Confirm the official `/opt/homebrew/bin/codex` still starts normally.
10. Confirm `/Users/jack/Documents/chatbot-grok-stt` remains unchanged.

## Definition of done

The task is complete only when:

- a custom Codex source build offers `realtime.transport = "local"`;
- the same Codex thread/model/tools receive local STT transcripts;
- Codex output is spoken by Siri TTS;
- barge-in works;
- cloud voice modes still work;
- the main chatbot and installed official Codex remain untouched;
- automated checks and real-device checks are reported separately;
- setup and rollback commands are documented.

## Agent ownership and handoff rule

Use one editing owner per repository:

- **Voice adapter owner:** edits only
  `/Users/jack/Documents/chatbot-grok-stt-isolated`.
- **Codex integration owner:** edits only
  `/Users/jack/Documents/codex-local-voice`.
- **Reviewers:** read-only unless explicitly assigned a separate worktree.

The two owners must agree on the adapter contract in Milestone 1 before working
in parallel. Do not let both agents edit the same repository or invent separate
message formats.

Do not commit, push, replace binaries, or merge unless Jack asks. Leave changes
reviewable in their assigned worktrees.

## First instruction to the next agent

Copy this exactly when handing the work to an implementation agent:

```text
Read /Users/jack/Documents/chatbot-grok-stt-isolated/VOICE-SWAP-HANDOFF.md completely before editing. Treat that absolute path as the single source of truth. This is a two-repository change. Do not implement the old Frameless event-renaming plan and do not point Codex at chatbot-grok-stt's existing /v1/realtime socket. Start with Milestone 0, then complete only Milestone 1 using a fake local voice adapter. Preserve the existing cloud realtime path. Stop after the fake transcript -> Codex handoff -> appendSpeech loop is proven, report the files changed and tests run, and do not commit or push.
```
