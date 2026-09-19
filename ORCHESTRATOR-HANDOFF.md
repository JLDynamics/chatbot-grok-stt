 # Orchestrator Handoff - Pi face + Luna voice plugin
 
 Reviewed against source by Claude on 2026-09-18: Pi-side facts and most chatbot-side facts confirmed exact. One correction folded into T1 below (audio I/O is client-only today, no independent server-side mic/speaker). Three open questions resolved with Jack, folded into "Decisions" below.
 
 Goal: Pi becomes the face and sole orchestrator. The chatbot voice stack becomes a dumb ears-and-mouth plugin launched from Pi with /voice. Copies the ChatGPT-voice pattern: fast talker up front, smart thinker in back, async.
 
 Worktree: /Users/jack/Documents/chatbot-grok-stt-orchestrator (detached HEAD fdb7fe4, from origin/main). Prototype here. Do NOT touch /Users/jack/Documents/chatbot-grok-stt main checkout.
 
 ## Agreed architecture
 
 1. Pi is the face. No Voice.app panel. /voice in Pi starts talk mode, quit stops it.
 2. Luna is ears + mouth only. On-device STT hears, Luna/Siri speaks, zero thinking. Transcript -> Pi, Pi answers, Luna speaks.
 3. Pi is the one boss. All tools, search, screenshots, subagents owned by Pi + Pi Crew. Strip Luna-side tools; nothing needs moving since Pi already has it.
 4. Async: Luna keeps chatting while Pi works, speaks result when ready. Voice never blocks on slow work.
 5. Writer/actor split: Pi writes the words, Luna reads them.
 
 ## Verified codebase facts (checked 2026-09-18)
 
 ### Chatbot voice stack
 - main checkout clean, branch main @ fdb7fe4 Restore read_page so the Chrome page bridge reaches the voice model.
 - Entry: chatbot serve in src/chatbot/cli.py -> chatbot/s2s_pipeline.py run_pipeline_command.
 - Realtime server: src/chatbot/api/openai_realtime/server.py class RealtimeServer, default ws://0.0.0.0:8766/v1/realtime via uvicorn. Router websocket_router.py, config runtime_config.py, pipeline src/chatbot/pipeline/.
 - run-browser.sh: starts realtime :8766 + sidecar :7860. Reads ~/.config/chatbot/env. Sets PYTHONPATH=<checkout>/src (critical for worktrees). Flags --reuse-running, --sidecar-only. Health http://127.0.0.1:8766/health and http://127.0.0.1:7860/api/config.
 - run-openrouter.sh: native-stt -> responses-api -> siri TTS. Defaults MODEL=openai/gpt-5.6-luna PORT=8766 WEB_PORT=7860 STT_LOCALE=en-US TTS=siri SIRI_VOICE=en-US-F SIRI_TTS_BIN=~/.local/bin/siri-tts. Requires OPENROUTER_API_KEY. Builds macos/SpeechHelper/build/speech-helper if missing.
 - Sidecar web_app/ :7860, chrome bridge web_app/chrome_article_bridge (real Chrome extension, background.js + content.js, ~880 lines - reads whatever tab the user already has open). CORRECTED: macos/SpeechHelper is just a headless stdin/stdout STT subprocess (nothing to drop). macos/Voice is NOT droppable as-is - its AudioEngine.swift + LiveVoiceBackend.swift are the ONLY mic capture and TTS playback client in the system (sends input_audio_buffer.append over the websocket, plays back reply audio). The Python server never touches the mic or speakers directly. See T1.
 
 ### Pi side (verified live)
 - Binary /opt/homebrew/bin/pi. Config ~/.pi/agent/settings.json: provider zenmux, model meta/muse-spark-1.3-contributor, thinking xhigh, packages git:github.com/badlogic/pi-skills (browser-tools) + npm:@melihmucuk/pi-crew.
 - Pi Crew ~/.pi/agent/pi-crew.json: worker, scout, planner, oracle, code-reviewer, quality-reviewer all zenmux/meta/muse-spark-1.3-contributor.
 - Skills ~/.pi/agent/skills/agent-note and browser-control, ext ~/.pi/agent/extensions/herdr-agent-state.ts. Sessions ~/.pi/agent/sessions/.
 - Useful flags: --provider --model --skill --extension --mode text|json|rpc --print/-p --session/--continue/--resume --tools/-t --exclude-tools/-xt --thinking.
 
 ## Build tasks
 
 ### T1 - /voice skill booting voice loop (headless Voice.app, not "no Voice.app")
 - CORRECTED SCOPE: Pi cannot be the websocket client - it has no mic capture or audio playback. What's needed is a headless BUILD of the existing macos/Voice target: keep AudioEngine.swift, LiveVoiceBackend.swift, VoiceRuntime.swift, LocalServiceStarter.swift exactly as-is; delete the SwiftUI window/hotkey layer (PanelController, FloatingPanel, ConversationView, SessionsView, SettingsView, OrbView, GlobalHotKey). This is real Swift work, size it accordingly.
 - Create Pi skill /voice launching the headless binary (which itself launches <worktree>/run-browser.sh --reuse-running - LocalServiceStarter.swift already does this exact on-demand start/health-probe/stop lifecycle today, reuse it, do not rebuild it). Wait for both health endpoints. Open audio loop mic -> STT -> Pi, Pi reply -> TTS.
 - v1: reuse realtime WebSocket transport as today (headless Voice binary <-> Python server); the NEW piece is a small text bridge between the headless binary and Pi (transcript out, reply text in) - see T2.
 - On exit kill services, verify ports free.
 - Accept: /voice starts from Pi, speak -> transcript -> Pi -> spoken reply, quit frees ports.
 
 ### T2 - Message pipe Pi <-> Luna (two pipes, Luna dumb)
 - Forward STT transcript / TranscriptionCompletedEvent (pipeline/events.py) -> Pi input (pi --print or RPC).
 - Back: Pi text -> Siri TTS (existing path). Luna prompt pass-through speak-only.
 - Keep barge-in: SpeechStartedEvent/SpeechStoppedEvent + SESSION_END in websocket_router.py (drain 10s, quarantine 180s, do not break) + pipeline/control.py.
 - Accept: with Luna tools disabled conversation still flows, interruptions cancel cleanly.
 
 ### T3 - Strip Luna tools, Pi owns everything
 - Disable Luna server tools: src/chatbot/LLM/server_tools.py (SERVER_TOOL_NAMES = bash, web_search, web_fetch, read_page, read_article, search_chat_history, remember, forget), curl_bash.py, responses_api_language_model.py handlers, ToolActivityEvent path. Leave STT+VAD+TTS.
 - DECIDED: retire web_app/chrome_article_bridge entirely, Pi's browser-tools skill is the one web-reading path (no fallback kept). One thing to verify before deleting the extension: the bridge reads whatever tab the user already has open, hands-free - confirm browser-tools can attach to an existing tab, not just drive its own separate session, or that capability is gone.
 - CAVEAT (do not skip): server_tools.py's own docstring says these tools were pulled in-process specifically to avoid a client round-trip ("client called sidecar, posted output back, asked for a new response... none of that is necessary"). Routing them to Pi reintroduces that round trip. Build T4's ack-filler alongside this task, not after, or every tool-using turn gets audibly slower the moment T3 ships.
 - CAVEAT: `screenshot` stays client-side today (VoiceTools.swift + ScreenCaptureKit) because macOS Screen Recording permission is granted per code identity - Pi's process is a different identity and will need its own grant, it does not inherit Voice.app's.
 - CAVEAT: `remember`/`forget` currently write Luna's own chat-history store (web_app/sessions.py). Moving them to Pi means picking ONE memory store as source of truth (Luna's sidecar store, Pi's own ~/.pi/agent/sessions/, or Jack's private-journal) - decide before wiring, don't let it end up in all three.
 - Use Pi equivalents: browser-tools, screenshots, pi-crew dispatch. Do not re-implement in voice stack.
 - Accept: Luna has no bash/curl/search/bridge tools; Pi answers web question via its own tools in /voice.
 
 ### T4 - Async (voice never waits)
 - Per-turn: instant ack filler (let me check that) when Pi task takes over ~1.5s, keep mic open, speak full answer on completion.
 - Overlap: new speech cancels pending Pi job via SESSION_END control path before new turn.
 - Accept: slow question acks immediately, full answer later, turn-taking intact.
 - Sequencing note: build this alongside T3, not strictly after - see T3 caveat above.
 
 ## Decisions (resolved with Jack, 2026-09-18)
 - Always-on vs on-demand services: on-demand, teardown on quit. Already implemented today by macos/Voice/Sources/Session/LocalServiceStarter.swift (spawns run-browser.sh --reuse-running on launch, stop() on quit, health-probe reuse logic) - carry this into the headless T1 build, do not rebuild it.
 - Chrome bridge read path: retire web_app/chrome_article_bridge, move fully to Pi browser-tools. See T3 caveat for the one capability to verify first.
 - Siri voice + STT locale: keep as-is for v1 - SIRI_VOICE=en-US-F (voice Luna speaks with) and STT_LOCALE=en-US (language the speech recognizer listens for). No change.
 - Cloud speech model fronting Codex today is unverified from backend side; not needed for build.
 
 ## Guardrails
 - Prototype only in worktree. Secrets in ~/.config/chatbot/env (600). Never commit keys. Repo is public JLDynamics/chatbot-grok-stt.
 - Verify via health endpoints + lsof listeners, not UI alone.
 - Explain simply, no jargon. Report automated checks separately from real mic/speaker results.
