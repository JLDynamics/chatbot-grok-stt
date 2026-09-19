import AVFoundation
import Foundation

/// Realtime WebSocket backend for the Chatbot voice server
/// (`ws://127.0.0.1:8766/v1/realtime`).
@MainActor
final class LiveVoiceBackend: VoiceBackend {

    var onState: ((SessionState) -> Void)?
    var onInputLevel: ((Float) -> Void)?
    var onOutputLevel: ((Float) -> Void)?
    var onUserSpeechStarted: (() -> Void)?
    var onTurnDropped: (() -> Void)?
    var onUserFinal: ((String, String?) -> Void)?
    var onAgentDelta: ((String) -> Void)?
    var onAgentDone: (() -> Void)?
    var onToolActive: ((String) -> Void)?
    var onToolDone: ((String, String) -> Void)?
    var onAudioStatus: ((String?) -> Void)?
    var onToolsCancelled: (() -> Void)?

    private enum Connection {
        case idle
        case starting
        case awaitingSession
        case ready
    }

    private let wsURL: URL
    private let voice: String
    private let instructions: String

    private let audio = AudioEngine()
    private let pcm = PCMBridge()
    private let mic = MicCapture()
    private let socket = VoiceSocketSend()
    private let micQueue = DispatchQueue(label: "com.jack.Voice.mic", qos: .userInteractive)
    private var webSocket: URLSessionWebSocketTask?
    private var receiveTask: Task<Void, Never>?
    private var handshakeTimeout: Task<Void, Never>?

    private var connection: Connection = .idle
    private var muted = false
    private var closed = true
    private var connectionGeneration = UUID()
    private let toolScope = VoiceWorkScope()
    private var seenToolCalls = Set<String>()
    private var responseCreateRequested = false
    /// Item id of the last finalized user turn, so an empty final can be
    /// matched to the turn it belongs to.
    private var lastInputItemId: String?
    /// Server-run tool calls in flight, by call_id, so the matching output can
    /// be reported under the tool's name.
    private var serverToolNames: [String: String] = [:]

    private var agentText = ""
    private var activeResponseId = ""
    /// A response.create that arrived while one was still streaming, deferred
    /// until it finishes. The server rejects an overlapping create outright, so
    /// sending it eagerly silently ended the turn.
    private var responseRequestPending = false
    /// Last time the server confirmed a new response. Arms the tool
    /// follow-up watchdog: if no response starts within seconds of a tool
    /// follow-up request, the create is re-sent once.
    private var lastResponseCreatedAt = Date.distantPast
    private var cancelledIds = Set<String>()
    private var lastOutputLevelAt = Date.distantPast
    /// Saved transcript, set by the controller before start() so the live
    /// session opens with the recent conversation in context.
    var historyMessages: [(role: String, text: String, name: String?)] = []

    init(
        url: URL,
        voice: String = "en-Emma_woman",
        instructions: String = """
        You are an AI conversation partner: perceptive, relaxed, warm, and quietly playful. You enjoy exploring ideas and have something thoughtful to contribute. Speak with the ease of someone comfortable in the conversation.
        """
    ) {
        self.wsURL = url
        self.voice = voice
        self.instructions = instructions
    }

    func setHistory(_ messages: [(role: String, text: String, name: String?)]) {
        historyMessages = messages
    }

    func refreshTools() {
        guard !closed, connection == .ready || connection == .awaitingSession else { return }
        sendSessionUpdate()
    }

    func start() async throws {
        await teardown(emitIdle: false)
        closed = false
        connection = .starting
        let generation = connectionGeneration
        mic.arm(generation: generation)
        onState?(.connecting)

        // Snapshot the mic gain once per session so the ~50 Hz tap never
        // touches UserDefaults (see PCMBridge.micGain).
        let configuredGain = UserDefaults.standard.object(forKey: "voice.micGain") as? Double ?? 0
        pcm.micGain = configuredGain > 0 ? Float(configuredGain) : 3.0

        onAudioStatus?("Starting local services…")
        do {
            try await LocalServiceStarter.shared.ensureReady(voice: wsURL, sidecar: LocalService.sidecarAPI)
        } catch {
            if connectionGeneration == generation { await teardown(emitIdle: false) }
            throw error
        }
        guard !closed, connectionGeneration == generation, !Task.isCancelled else { return }
        onAudioStatus?(nil)

        // Open the socket before the audio engine: the TCP/WebSocket handshake
        // and session.created happen on the network while voice-processing
        // setup (a few hundred ms) runs on this actor, instead of one after
        // the other. Mic frames only flow once `connection == .ready`, and a
        // socket failure meanwhile tears everything down via fail().
        openWebSocket(generation: generation)

        do {
            try await audio.start()
        } catch {
            fail(error.localizedDescription)
            throw error
        }

        if closed || generation != connectionGeneration || Task.isCancelled {
            audio.stop()
            return
        }

        onAudioStatus?(audio.statusNote)
        audio.onPlaybackDrained = { [weak self] in
            Task { @MainActor in
                guard let self, !self.closed, self.connectionGeneration == generation,
                      self.activeResponseId.isEmpty, !self.audio.isPlaying else { return }
                self.onOutputLevel?(0)
                self.onState?(.listening)
            }
        }
        let bridge = pcm
        let capture = mic
        let sink = socket
        let queue = micQueue
        audio.onInputLevel = { [weak self] level in
            Task { @MainActor in
                guard let self, self.connectionGeneration == generation, !self.closed else { return }
                self.onInputLevel?(capture.isGateOpen ? level : 0)
            }
        }
        audio.onBuffer = { buffer in
            guard let bytes = bridge.micPCM16(from: buffer) else {
                NSLog("[Tap] micPCM16 returned nil for frames=\(buffer.frameLength)")
                return
            }
            queue.async {
                guard let chunk = capture.ingest(bytes, generation: generation) else { return }
                sink.send(
                    ["type": "input_audio_buffer.append", "audio": Data(chunk).base64EncodedString()],
                    generation: generation
                )
            }
        }
    }

    private func openWebSocket(generation: UUID) {
        let task = URLSession.shared.webSocketTask(with: wsURL)
        webSocket = task
        socket.attach(task, generation: generation)
        connection = .awaitingSession
        task.resume()
        let decoder = pcm
        receiveTask = Task.detached { [weak self] in
            await Self.pump(task, owner: self, pcm: decoder, generation: generation)
        }
        handshakeTimeout = Task { [weak self] in
            try? await Task.sleep(nanoseconds: 8_000_000_000)
            guard !Task.isCancelled, let self, self.connectionGeneration == generation,
                  self.connection == .awaitingSession else { return }
            self.fail("Can't reach the service")
        }
    }

    func stop() async {
        await teardown(emitIdle: true)
    }

    func setMuted(_ muted: Bool) {
        self.muted = muted
        mic.setMuted(muted)
        audio.setMuted(muted)
        if muted { onInputLevel?(0) }
    }

    func interrupt() {
        cancelToolWork()
        rememberCancelled(activeResponseId)
        send(["type": "response.cancel"])
        audio.clearPlayback()
        onOutputLevel?(0)
        finishAgentTurn()
        if !closed { onState?(.listening) }
    }

    func speak(_ text: String) {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty, !closed else { return }
        send(["type": "response.speak", "text": trimmed])
    }

    private func teardown(emitIdle: Bool) async {
        closed = true
        connectionGeneration = UUID()
        cancelToolWork()
        seenToolCalls.removeAll()
        responseCreateRequested = false
        lastInputItemId = nil
        connection = .idle
        handshakeTimeout?.cancel()
        handshakeTimeout = nil
        receiveTask?.cancel()
        receiveTask = nil
        webSocket?.cancel(with: .goingAway, reason: nil)
        webSocket = nil
        socket.attach(nil, generation: connectionGeneration)
        audio.onBuffer = nil
        audio.onInputLevel = nil
        audio.onPlaybackDrained = nil
        onAudioStatus?(nil)
        audio.stop()
        pcm.reset()
        mic.disarm()
        agentText = ""
        activeResponseId = ""
        responseRequestPending = false
        cancelledIds.removeAll()
        muted = false
        onInputLevel?(0)
        onOutputLevel?(0)
        if emitIdle { onState?(.idle) }
    }

    private func fail(_ message: String) {
        guard !closed else { return }
        closed = true
        connectionGeneration = UUID()
        cancelToolWork()
        seenToolCalls.removeAll()
        responseCreateRequested = false
        lastInputItemId = nil
        connection = .idle
        handshakeTimeout?.cancel()
        handshakeTimeout = nil
        receiveTask?.cancel()
        receiveTask = nil
        webSocket?.cancel(with: .goingAway, reason: nil)
        webSocket = nil
        socket.attach(nil, generation: connectionGeneration)
        audio.onBuffer = nil
        audio.onInputLevel = nil
        audio.onPlaybackDrained = nil
        onAudioStatus?(nil)
        audio.stop()
        pcm.reset()
        mic.disarm()
        onInputLevel?(0)
        onOutputLevel?(0)
        onState?(.failed(message))
    }

    /// Receive off the MainActor. URLSession delivers on the session queue;
    /// awaiting receive() on MainActor deadlocks connecting forever.
    /// Audio deltas are decoded here so JSON/base64/resample do not hitch the panel.
    private static func pump(
        _ ws: URLSessionWebSocketTask,
        owner: LiveVoiceBackend?,
        pcm: PCMBridge,
        generation: UUID
    ) async {
        while !Task.isCancelled {
            let closed = await MainActor.run { owner?.closed != false || owner?.connectionGeneration != generation }
            if closed { break }
            do {
                let message = try await ws.receive()
                let text: String?
                switch message {
                case .string(let value): text = value
                case .data(let data): text = String(data: data, encoding: .utf8)
                @unknown default: text = nil
                }
                guard let text else { continue }
                if let delta = parseAudioDelta(text) {
                    let dest = await MainActor.run { () -> AVAudioFormat? in
                        guard let owner, owner.connectionGeneration == generation, !owner.closed else { return nil }
                        if owner.shouldDropAudio(responseId: delta.responseId) { return nil }
                        return owner.audio.playbackFormat
                    }
                    guard let dest, let buffer = pcm.playbackBuffer(base64: delta.b64, dest: dest) else { continue }
                    let level = pcm.rmsLevel(buffer)
                    await MainActor.run {
                        owner?.playDecodedAudio(buffer, level: level, responseId: delta.responseId, generation: generation)
                    }
                } else {
                    await MainActor.run {
                        guard owner?.connectionGeneration == generation else { return }
                        owner?.handle(.string(text))
                    }
                }
            } catch {
                NSLog("[LiveVoice] receive failed: \(error.localizedDescription)")
                await MainActor.run {
                    guard !Task.isCancelled, owner?.connectionGeneration == generation else { return }
                    owner?.fail("Can't reach the service")
                }
                break
            }
        }
    }

    private struct AudioDelta {
        let responseId: String
        let b64: String
    }

    private static func parseAudioDelta(_ text: String) -> AudioDelta? {
        guard let data = text.data(using: .utf8),
              let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let type = json["type"] as? String,
              type == "response.audio.delta" || type == "response.output_audio.delta",
              let b64 = json["delta"] as? String, !b64.isEmpty
        else { return nil }
        let responseId: String
        if let id = json["response_id"] as? String {
            responseId = id
        } else if let response = json["response"] as? [String: Any], let id = response["id"] as? String {
            responseId = id
        } else {
            responseId = ""
        }
        return AudioDelta(responseId: responseId, b64: b64)
    }

    private func shouldDropAudio(responseId: String) -> Bool {
        !responseId.isEmpty && cancelledIds.contains(responseId)
    }

    private func playDecodedAudio(
        _ buffer: AVAudioPCMBuffer,
        level: Float,
        responseId: String,
        generation: UUID
    ) {
        guard connectionGeneration == generation, !closed, !shouldDropAudio(responseId: responseId) else { return }
        publishOutputLevel(level)
        audio.play(buffer)
        onState?(.agentSpeaking)
    }

    private func handle(_ message: URLSessionWebSocketTask.Message) {
        switch message {
        case .string(let text):
            handleMessage(text)
        case .data(let data):
            if let text = String(data: data, encoding: .utf8) {
                handleMessage(text)
            }
        @unknown default:
            break
        }
    }

    private func handleMessage(_ text: String) {
        guard let data = text.data(using: .utf8),
              let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let type = json["type"] as? String
        else { return }

        switch type {
        case "session.created":
            sendSessionUpdate()
            replayHistory()
            connection = .ready
            mic.setAccepting(true)
            handshakeTimeout?.cancel()
            handshakeTimeout = nil
            if !closed { onState?(.listening) }

        case "response.created":
            responseCreateRequested = false
            lastResponseCreatedAt = Date()
            if let response = json["response"] as? [String: Any],
               let id = response["id"] as? String {
                activeResponseId = id
            }

        case "input_audio_buffer.speech_started":
            onUserSpeechStarted?()
            // The server sends cancellation first for confirmed interruptions.
            // A short continuation may emit speech_started without cancelling.
            // TTS chunk gaps must not flip the panel to Listening — that was
            // resizing the transcript and bouncing the conversation scroller.
            let responseActive = !activeResponseId.isEmpty
            if VoiceSpeechStartPolicy.shouldInterruptLocalPlayback(responseActive: responseActive) {
                cancelToolWork()
                if audio.isPlaying {
                    rememberCancelled(activeResponseId)
                    audio.clearPlayback()
                    onOutputLevel?(0)
                    finishAgentTurn()
                }
            }
            if !closed,
               VoiceSpeechStartPolicy.shouldShowListening(
                   responseActive: responseActive,
                   playing: audio.isPlaying
               ) {
                onState?(.listening)
            }

        case "input_audio_buffer.speech_stopped":
            // The turn is over on the server's side: show that the model is
            // working, not that the panel is idly listening. speech_started,
            // turn_ignored, audio or response.done all move it on.
            if !closed,
               VoiceSpeechStartPolicy.shouldShowListening(
                   responseActive: !activeResponseId.isEmpty,
                   playing: audio.isPlaying
               ) {
                onState?(.thinking)
            }

        case "conversation.item.input_audio_transcription.completed":
            let itemId = json["item_id"] as? String
            let transcript = (json["transcript"] as? String ?? "")
                .trimmingCharacters(in: .whitespacesAndNewlines)
            if transcript.isEmpty {
                // An empty final must not call onUserFinal — that would commit
                // an empty bubble.
                break
            }
            lastInputItemId = itemId
            onUserFinal?(transcript, itemId)

        case "response.audio_transcript.delta", "response.output_audio_transcript.delta":
            if cancelledIds.contains(responseId(in: json)) { return }
            if let delta = json["delta"] as? String, !delta.isEmpty {
                pushAgentDelta(delta)
            }

        case "response.audio.delta", "response.output_audio.delta":
            let rid = responseId(in: json)
            if !rid.isEmpty, cancelledIds.contains(rid) { return }
            if let b64 = json["delta"] as? String,
               let buffer = pcm.playbackBuffer(base64: b64, dest: audio.playbackFormat) {
                publishOutputLevel(pcm.rmsLevel(buffer))
                audio.play(buffer)
            }
            onState?(.agentSpeaking)

        case "response.done":
            if let response = json["response"] as? [String: Any],
               let id = response["id"] as? String {
                if !activeResponseId.isEmpty, id != activeResponseId { return }
                if id == activeResponseId { activeResponseId = "" }
                if response["status"] as? String == "cancelled" {
                    rememberCancelled(id)
                    cancelToolWork()
                    audio.clearPlayback()
                }
            }
            if !serverToolNames.isEmpty {
                // A server-run tool whose output never arrived (the turn was
                // interrupted mid-call) must not leave the pill spinning.
                serverToolNames.removeAll()
                onToolsCancelled?()
            }
            finishAgentTurn()
            if !audio.isPlaying {
                onOutputLevel?(0)
                if !closed { onState?(.listening) }
            }
            if responseRequestPending,
               VoiceToolFollowUp.shouldSend(
                   pendingTools: toolScope.pendingIds.count,
                   responseActive: !activeResponseId.isEmpty
               ) {
                responseRequestPending = false
                sendResponseCreate()
            }

        case "response.function_call_arguments.done":
            if cancelledIds.contains(responseId(in: json)) { return }
            guard let name = json["name"] as? String,
                  let callId = json["call_id"] as? String
            else { break }
            let argsJson = json["arguments"] as? String ?? "{}"
            executeTool(name: name, argsJson: argsJson, callId: callId)

        case "conversation.item.created":
            // Tools the server ran inside the response arrive as the items they
            // created: a function_call when the call starts, its
            // function_call_output when it finishes. Show them; never run them.
            guard let item = json["item"] as? [String: Any],
                  let callId = item["call_id"] as? String else { break }
            if item["type"] as? String == "function_call", let name = item["name"] as? String {
                serverToolNames[callId] = name
                onToolActive?(name)
            } else if item["type"] as? String == "function_call_output",
                      let name = serverToolNames.removeValue(forKey: callId) {
                onToolDone?(name, item["output"] as? String ?? "")
            }

        case "error":
            // Transport close is fatal; server events like turn_ignored are not.
            if let error = json["error"] as? [String: Any], error["code"] as? String == "turn_ignored" {
                onTurnDropped?()
                if !closed, !audio.isPlaying, activeResponseId.isEmpty {
                    onState?(.listening)
                }
            }

        default:
            break
        }
    }

    /// SessionController appends with `+=`, so only new text may be forwarded.
    /// If the server sends cumulative text, strip the prefix we already have.
    private func pushAgentDelta(_ incoming: String) {
        let chunk: String
        if incoming.hasPrefix(agentText), incoming.count >= agentText.count {
            chunk = String(incoming.dropFirst(agentText.count))
            agentText = incoming
        } else {
            chunk = incoming
            agentText += incoming
        }
        guard !chunk.isEmpty else { return }
        onAgentDelta?(chunk)
        onState?(.agentSpeaking)
    }

    private func finishAgentTurn() {
        agentText = ""
        onAgentDone?()
    }

    private func rememberCancelled(_ id: String) {
        guard !id.isEmpty else { return }
        cancelledIds.insert(id)
        if cancelledIds.count > 32, let oldest = cancelledIds.first {
            cancelledIds.remove(oldest)
        }
    }

    private func responseId(in json: [String: Any]) -> String {
        if let id = json["response_id"] as? String { return id }
        if let response = json["response"] as? [String: Any],
           let id = response["id"] as? String {
            return id
        }
        return ""
    }

    private func publishOutputLevel(_ level: Float) {
        let now = Date()
        guard now.timeIntervalSince(lastOutputLevelAt) > 1.0 / 30 else { return }
        lastOutputLevelAt = now
        onOutputLevel?(level)
    }

    private func sendSessionUpdate() {
        // Persona only. The server's system prompt adds the research guidance,
        // the date and the personal-memory profile; the sidecar owns the page
        // ladder. Nothing about tools needs to be re-sent from here.
        let tools = VoiceToolExecutor.shared.activeToolDefinitions()
        var sess: [String: Any] = [
            "type": "realtime",
            "instructions": instructions,
            "audio": ["output": ["voice": voice]],
        ]
        let thinker = ProcessInfo.processInfo.environment["VOICE_THINKER"]?.lowercased()
        if thinker == "luna" || thinker == "pi" {
            sess["thinker"] = thinker as Any
        }
        // Pi owns tools. Sending Luna's client tools would invite a think
        // follow-up this session must not start.
        if thinker != "pi", !tools.isEmpty {
            sess["tools"] = tools
            sess["tool_choice"] = "auto"
        }
        send([
            "type": "session.update",
            "session": sess,
        ])
    }

    /// Replay the saved transcript tail into the live conversation, mirroring
    /// the web `_replayHistory` (last 20 user/assistant/tool messages).
    private func replayHistory() {
        var replayable: [(role: String, text: String)] = []
        for m in historyMessages {
            let text = m.text.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !text.isEmpty else { continue }
            if m.role == "user" || m.role == "assistant" {
                replayable.append((m.role, text))
            } else if m.role == "tool" {
                let name = (m.name ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
                let shown = text.count > 500 ? String(text.prefix(497)) + "..." : text
                replayable.append(("assistant", "[Earlier I used \(name.isEmpty ? "tool" : name)] \(shown)"))
            }
        }
        for m in replayable.suffix(20) {
            let type = m.role == "assistant" ? "output_text" : "input_text"
            send([
                "type": "conversation.item.create",
                "item": [
                    "type": "message",
                    "role": m.role,
                    "content": [["type": type, "text": m.text]],
                ] as [String: Any],
            ])
        }
        if !replayable.isEmpty {
            NSLog("[LiveVoice] replayed %d saved message(s)", min(replayable.count, 20))
        }
    }

    private func cancelToolWork() {
        if !closed {
            for callId in toolScope.pendingIds {
                sendToolOutput(callId: callId, output: "This tool was cancelled because the conversation moved on. Its result is unavailable.")
            }
        }
        toolScope.cancel()
        serverToolNames.removeAll()
        onToolsCancelled?()
        responseRequestPending = false
    }

    /// Run a tool the server forwarded to the client (screenshot),
    /// post its output and ask the model to continue.
    private func executeTool(name: String, argsJson: String, callId: String) {
        guard !closed, seenToolCalls.insert(callId).inserted else { return }
        let generation = toolScope.generation
        NSLog("[LiveVoice] tool call: %@ id=%@", name, callId)
        onToolActive?(name)
        let task = Task {
            let result = await VoiceToolExecutor.shared.run(name: name, argsJson: argsJson)
            guard !Task.isCancelled, !self.closed, self.toolScope.generation == generation else {
                NSLog("[LiveVoice] tool %@ dropping result pre-send: cancelled=%@ closed=%@ genMatch=%@",
                      name, "\(Task.isCancelled)", "\(!self.closed)", "\(self.toolScope.generation == generation)")
                return
            }
            NSLog("[LiveVoice] tool %@ done: output=%d chars image=%@", name, result.output.count, result.image == nil ? "no" : "yes")
            self.sendToolOutput(callId: callId, output: result.output)
            if let image = result.image { self.sendUserImage(dataUrl: image) }
            guard !Task.isCancelled, !self.closed, self.toolScope.generation == generation else {
                NSLog("[LiveVoice] tool %@ dropping follow-up post-send: cancelled=%@ closed=%@ genMatch=%@",
                      name, "\(Task.isCancelled)", "\(!self.closed)", "\(self.toolScope.generation == generation)")
                return
            }
            self.toolScope.finish(callId, generation: generation)
            self.onToolDone?(name, result.output)
            self.requestResponse()
            if self.toolScope.pendingIds.isEmpty {
                self.armFollowUpWatchdog(callId: callId, generation: generation)
            }
        }
        toolScope.insert(task, id: callId)
    }

    private func sendToolOutput(callId: String, output: String) {
        send([
            "type": "conversation.item.create",
            "item": [
                "type": "function_call_output",
                "call_id": callId,
                "output": output,
            ] as [String: Any],
        ])
    }

    private func sendUserImage(dataUrl: String) {
        send([
            "type": "conversation.item.create",
            "item": [
                "type": "message",
                "role": "user",
                "content": [
                    ["type": "input_image", "image_url": dataUrl]
                ],
            ] as [String: Any],
        ])
    }

    /// Ask the model to continue after a tool result.
    ///
    /// A tool that returns faster than the in-flight response finishes (a
    /// bridge miss answers immediately) would otherwise race it, and the server
    /// answers an overlapping create with
    /// `conversation_already_has_active_response`. Nothing retried that, so the
    /// turn died after the spoken acknowledgement and the tool result was never
    /// used — which also stops any fallback chain at its first rung. Defer
    /// instead, and flush on `response.done`.
    ///
    /// Parallel tools in one response must also wait for each other. Sending a
    /// follow-up after the first of several `web_search` results makes the
    /// model answer twice with the same content once the rest arrive.
    private func requestResponse() {
        guard toolScope.pendingIds.isEmpty else {
            NSLog("[LiveVoice] follow-up waiting for %d remaining tool(s)", toolScope.pendingIds.count)
            return
        }
        guard activeResponseId.isEmpty else {
            NSLog("[LiveVoice] follow-up deferred (response %@ active)", activeResponseId)
            responseRequestPending = true
            return
        }
        if responseCreateRequested {
            NSLog("[LiveVoice] follow-up deferred (create already requested)")
            responseRequestPending = true
            return
        }
        NSLog("[LiveVoice] follow-up: sending response.create now")
        sendResponseCreate()
    }

    private func sendResponseCreate() {
        guard !closed, !responseCreateRequested else {
            NSLog("[LiveVoice] sendResponseCreate skipped: closed=%@ alreadyRequested=%@",
                  "\(!closed)", "\(responseCreateRequested)")
            return
        }
        responseCreateRequested = true
        NSLog("[LiveVoice] sending response.create (follow-up)")
        send([
            "type": "response.create",
            "response": [:] as [String: Any],
        ])
    }

    /// Retry a tool follow-up once if the server never starts a response.
    /// Covers silent drops between tool completion and response.created
    /// (e.g. a create lost in an echo-cancel race). Stands down on any newer
    /// response, a superseding generation, or close. A duplicate create that
    /// arrives after the server already started one is rejected by the server
    /// and ignored by the client, so the retry is safe.
    private func armFollowUpWatchdog(callId: String, generation: UUID) {
        let requestedAt = Date()
        DispatchQueue.main.asyncAfter(deadline: .now() + 10) { [weak self] in
            guard let self, !self.closed, self.toolScope.generation == generation else {
                NSLog("[LiveVoice] follow-up watchdog %@: stood down (superseded)", callId)
                return
            }
            guard self.lastResponseCreatedAt < requestedAt else {
                NSLog("[LiveVoice] follow-up watchdog %@: server responded, stood down", callId)
                return
            }
            NSLog("[LiveVoice] follow-up watchdog %@: no response in 10s, resending response.create", callId)
            self.responseCreateRequested = false
            self.responseRequestPending = false
            self.sendResponseCreate()
        }
    }

    private func send(_ object: [String: Any]) {
        socket.send(object)
    }
}

/// Resample / PCM16 helpers. Called from the audio tap thread, so not MainActor.
private final class PCMBridge: @unchecked Sendable {
    private let playLock = NSLock()
    private var playConverter: AVAudioConverter?
    private var playOutRate: Double = 0
    private let micRate: Double = 16_000
    private let ttsRate: Double = 24_000
    private let gainLock = NSLock()
    private var _micGain: Float = 3.0

    /// Mic input gain, read from defaults once per session (see start()).
    /// It used to be read inside micPCM16, i.e. a synchronized UserDefaults
    /// lookup on every mic-tap buffer (~50 Hz on the audio thread).
    var micGain: Float {
        get {
            gainLock.lock()
            defer { gainLock.unlock() }
            return _micGain
        }
        set {
            gainLock.lock()
            defer { gainLock.unlock() }
            _micGain = newValue
        }
    }

    func reset() {
        playLock.lock()
        playConverter = nil
        playOutRate = 0
        playLock.unlock()
    }

    func micPCM16(from buffer: AVAudioPCMBuffer) -> [UInt8]? {
        // No shared mutable state here; keep off the playback lock so the
        // ~50Hz mic tap never blocks audio deltas (and vice versa).

        let inFormat = buffer.format
        let inFrames = Int(buffer.frameLength)
        let chCount = Int(inFormat.channelCount)
        guard inFrames > 0, inFormat.sampleRate > 0, chCount > 0 else { return nil }

        // Calibrated mic input gain. macOS input volume is often set low and
        // browsers compensate with AGC; we get the raw tap, so apply a
        // modest software boost (with hard clamping below). Override with:
        //   defaults write com.jack.Voice voice.micGain -float 4.0
        // Cached per session — never read UserDefaults in this hot path.
        let gain = micGain
        let ratio = micRate / inFormat.sampleRate
        let outFrames = max(1, Int((Double(inFrames) * ratio).rounded()))
        var pcm = [Int16](repeating: 0, count: outFrames)

        func sample(at frame: Int) -> Float {
            let i = max(0, min(frame, inFrames - 1))
            if let floats = buffer.floatChannelData {
                var mixed: Float = 0
                for c in 0..<chCount { mixed += floats[c][i] }
                return mixed / Float(chCount)
            }
            if let ints = buffer.int16ChannelData {
                var mixed: Float = 0
                for c in 0..<chCount { mixed += Float(ints[c][i]) / Float(Int16.max) }
                return mixed / Float(chCount)
            }
            return 0
        }

        if abs(ratio - 1.0) < 0.001 {
            for i in 0..<outFrames {
                let s = max(-1, min(1, sample(at: i) * gain))
                pcm[i] = Int16(s * Float(Int16.max))
            }
        } else {
            for i in 0..<outFrames {
                let src = Double(i) / ratio
                let i0 = Int(src)
                let i1 = min(i0 + 1, inFrames - 1)
                let frac = Float(src - Double(i0))
                let mixed = sample(at: i0) * (1 - frac) + sample(at: i1) * frac
                let s = max(-1, min(1, mixed * gain))
                pcm[i] = Int16(s * Float(Int16.max))
            }
        }

        return pcm.withUnsafeBufferPointer { ptr in
            Array(UnsafeRawBufferPointer(start: ptr.baseAddress, count: outFrames * 2))
        }
    }

    func playbackBuffer(base64 b64: String, dest: AVAudioFormat) -> AVAudioPCMBuffer? {
        playLock.lock()
        defer { playLock.unlock() }

        guard dest.sampleRate > 0 else { return nil }
        guard let data = Data(base64Encoded: b64), data.count >= 2 else { return nil }
        let sampleCount = data.count / MemoryLayout<Int16>.size
        guard sampleCount > 0 else { return nil }

        guard let srcFormat = AVAudioFormat(
            commonFormat: .pcmFormatInt16,
            sampleRate: ttsRate,
            channels: 1,
            interleaved: true
        ) else { return nil }

        guard let src = AVAudioPCMBuffer(
            pcmFormat: srcFormat,
            frameCapacity: AVAudioFrameCount(sampleCount)
        ) else { return nil }
        src.frameLength = AVAudioFrameCount(sampleCount)
        guard let dst = src.int16ChannelData?[0] else { return nil }
        data.copyBytes(
            to: UnsafeMutableRawBufferPointer(start: UnsafeMutableRawPointer(dst), count: sampleCount * 2)
        )

        if playConverter == nil || abs(playOutRate - dest.sampleRate) > 0.5 {
            playConverter = AVAudioConverter(from: srcFormat, to: dest)
            playOutRate = dest.sampleRate
        }
        guard let converter = playConverter else { return nil }

        let ratio = dest.sampleRate / ttsRate
        let outFrames = AVAudioFrameCount(Double(sampleCount) * ratio) + 32
        guard let out = AVAudioPCMBuffer(pcmFormat: dest, frameCapacity: outFrames) else { return nil }

        var error: NSError?
        var consumed = false
        converter.convert(to: out, error: &error) { _, status in
            if consumed {
                status.pointee = .noDataNow
                return nil
            }
            consumed = true
            status.pointee = .haveData
            return src
        }
        if error != nil || out.frameLength == 0 { return nil }
        return out
    }

    func rmsLevel(_ buffer: AVAudioPCMBuffer) -> Float {
        guard let ch = buffer.floatChannelData?[0] else { return 0 }
        let n = Int(buffer.frameLength)
        guard n > 0 else { return 0 }
        var sum: Float = 0
        for i in 0..<n { sum += ch[i] * ch[i] }
        let rms = (sum / Float(n)).squareRoot()
        let db = 20 * log10(max(rms, 0.000_001))
        return max(0, min(1, (db + 50) / 50))
    }
}
