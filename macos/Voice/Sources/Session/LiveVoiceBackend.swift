import AVFoundation
import Foundation

/// Realtime WebSocket backend for the Chatbot voice server
/// (`ws://127.0.0.1:8766/v1/realtime`).
@MainActor
final class LiveVoiceBackend: VoiceBackend {

    var onState: ((SessionState) -> Void)?
    var onInputLevel: ((Float) -> Void)?
    var onOutputLevel: ((Float) -> Void)?
    var onUserPartial: ((String) -> Void)?
    var onUserFinal: ((String, String?) -> Void)?
    var onAgentDelta: ((String) -> Void)?
    var onAgentDone: (() -> Void)?
    var onToolActive: ((String) -> Void)?
    var onToolDone: ((String) -> Void)?

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
    private let noiseGate = PCM16NoiseGate()
    private var webSocket: URLSessionWebSocketTask?
    private var receiveTask: Task<Void, Never>?
    private var handshakeTimeout: Task<Void, Never>?

    private var connection: Connection = .idle
    private var muted = false
    private var closed = true

    private var agentText = ""
    private var activeResponseId = ""
    private var cancelledIds = Set<String>()
    private var lastOutputLevelAt = Date.distantPast
    /// Personal memory profile + saved transcript, set by the controller
    /// before start() so the live session opens with the same context as web.
    var memoryProfile = ""
    var historyMessages: [(role: String, text: String, name: String?)] = []

    init(
        url: URL,
        voice: String = "en-Emma_woman",
        instructions: String =
            "You are a spoken assistant. Short sentences. No lists, markdown, or emoji."
    ) {
        self.wsURL = url
        self.voice = voice
        self.instructions = instructions
    }

    func setHistory(_ messages: [(role: String, text: String, name: String?)]) {
        historyMessages = messages
    }

    func refreshMemory() async {
        await refreshInstructions()
    }

    func start() async throws {
        await teardown(emitIdle: false)
        closed = false
        connection = .starting
        onState?(.connecting)

        // Best-effort personal memory so the assistant knows the user like web.
        // Never blocks voice: failure just means an empty profile this session.
        if let profile = try? await ChatStore.shared.getPersonalMemory() {
            memoryProfile = profile
        }

        do {
            try await audio.start()
        } catch {
            connection = .idle
            closed = true
            onState?(.failed(error.localizedDescription))
            throw error
        }

        if closed {
            audio.stop()
            connection = .idle
            return
        }

        let bridge = pcm
        audio.onInputLevel = { [weak self] level in
            Task { @MainActor in self?.onInputLevel?(level) }
        }
        audio.onBuffer = { [weak self] buffer in
            guard let bytes = bridge.micPCM16(from: buffer) else {
                NSLog("[Tap] micPCM16 returned nil for frames=\(buffer.frameLength)")
                return
            }
            Task { @MainActor in self?.appendMicPCM(bytes) }
        }

        let session = URLSession.shared
        let task = session.webSocketTask(with: wsURL)
        webSocket = task
        connection = .awaitingSession
        task.resume()
        receiveTask = Task.detached { [weak self] in
            await Self.pump(task, owner: self)
        }
        handshakeTimeout = Task { [weak self] in
            try? await Task.sleep(nanoseconds: 8_000_000_000)
            guard let self, self.connection == .awaitingSession else { return }
            self.fail("Can't reach the service")
        }
    }

    func stop() async {
        await teardown(emitIdle: true)
    }

    func setMuted(_ muted: Bool) {
        self.muted = muted
        audio.setMuted(muted)
        if muted { onInputLevel?(0) }
    }

    func interrupt() {
        rememberCancelled(activeResponseId)
        send(["type": "response.cancel"])
        audio.clearPlayback()
        onOutputLevel?(0)
        finishAgentTurn()
        if !closed { onState?(.listening) }
    }

    private func teardown(emitIdle: Bool) async {
        closed = true
        connection = .idle
        handshakeTimeout?.cancel()
        handshakeTimeout = nil
        receiveTask?.cancel()
        receiveTask = nil
        webSocket?.cancel(with: .goingAway, reason: nil)
        webSocket = nil
        audio.onBuffer = nil
        audio.onInputLevel = nil
        audio.stop()
        pcm.reset()
        noiseGate.reset()
        agentText = ""
        activeResponseId = ""
        cancelledIds.removeAll()
        muted = false
        micFramesSent = 0
        micPending.removeAll(keepingCapacity: true)
        onInputLevel?(0)
        onOutputLevel?(0)
        if emitIdle { onState?(.idle) }
    }

    private func fail(_ message: String) {
        guard !closed else { return }
        closed = true
        connection = .idle
        handshakeTimeout?.cancel()
        handshakeTimeout = nil
        receiveTask?.cancel()
        receiveTask = nil
        webSocket?.cancel(with: .goingAway, reason: nil)
        webSocket = nil
        audio.onBuffer = nil
        audio.onInputLevel = nil
        audio.stop()
        pcm.reset()
        noiseGate.reset()
        onInputLevel?(0)
        onOutputLevel?(0)
        onState?(.failed(message))
    }

    private var micFramesSent = 0
    /// Batches tap buffers (~21ms) up to ~40ms per WS message, matching the
    /// browser client's chunk size and halving MainActor/WS overhead.
    private var micPending = [UInt8]()
    private let micSendThreshold = 1280 // 40ms of 16kHz PCM16 mono

    private func appendMicPCM(_ rawBytes: [UInt8]) {
        guard connection == .ready, !muted, !closed else {
            if micFramesSent == 0 {
                NSLog("[MicPCM] dropped initial frame: conn=\(connection) muted=\(muted) closed=\(closed)")
            }
            return
        }
        let (bytes, _) = noiseGate.process(rawBytes)
        micPending.append(contentsOf: bytes)
        guard micPending.count >= micSendThreshold else { return }
        let chunk = micPending
        micPending.removeAll(keepingCapacity: true)
        micFramesSent += 1
        if micFramesSent % 25 == 1 {
            NSLog("[LiveVoice] mic streaming frame #%d (bytes=%d)", micFramesSent, chunk.count)
        }
        send(["type": "input_audio_buffer.append", "audio": Data(chunk).base64EncodedString()])
    }

    /// Receive off the MainActor. URLSession delivers on the session queue;
    /// awaiting receive() on MainActor deadlocks connecting forever.
    private static func pump(_ ws: URLSessionWebSocketTask, owner: LiveVoiceBackend?) async {
        while !Task.isCancelled {
            let closed = await MainActor.run { owner?.closed ?? true }
            if closed { break }
            do {
                let message = try await ws.receive()
                await MainActor.run { owner?.handle(message) }
            } catch {
                NSLog("[LiveVoice] receive failed: \(error.localizedDescription)")
                await MainActor.run { owner?.fail("Can't reach the service") }
                break
            }
        }
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
            handshakeTimeout?.cancel()
            handshakeTimeout = nil
            if !closed { onState?(.listening) }

        case "response.created":
            if let response = json["response"] as? [String: Any],
               let id = response["id"] as? String {
                activeResponseId = id
            }

        case "input_audio_buffer.speech_started":
            if audio.isPlaying {
                rememberCancelled(activeResponseId)
                audio.clearPlayback()
                onOutputLevel?(0)
                finishAgentTurn()
            }
            if !closed { onState?(.listening) }

        case "conversation.item.input_audio_transcription.delta":
            if let delta = json["delta"] as? String, !delta.isEmpty {
                onUserPartial?(delta)
            }

        case "conversation.item.input_audio_transcription.completed":
            if let transcript = json["transcript"] as? String, !transcript.isEmpty {
                onUserFinal?(transcript, json["item_id"] as? String)
            }

        case "response.audio_transcript.delta", "response.output_audio_transcript.delta":
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
                if id == activeResponseId { activeResponseId = "" }
                cancelledIds.remove(id)
            }
            finishAgentTurn()
            onOutputLevel?(0)
            if !closed { onState?(.listening) }

        case "response.function_call_arguments.done":
            guard let name = json["name"] as? String,
                  let callId = json["call_id"] as? String
            else { break }
            let argsJson = json["arguments"] as? String ?? "{}"
            executeTool(name: name, argsJson: argsJson, callId: callId)

        case "error":
            // Transport close is fatal; server events like turn_ignored are not.
            break

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
        let tools = VoiceToolExecutor.shared.activeToolDefinitions()
        let inst = VoiceToolExecutor.shared.effectiveInstructions(
            base: instructions, memoryProfile: memoryProfile)
        var sess: [String: Any] = [
            "type": "realtime",
            "instructions": inst,
            "audio": ["output": ["voice": voice]],
        ]
        if !tools.isEmpty {
            sess["tools"] = tools
            sess["tool_choice"] = "auto"
        }
        send([
            "type": "session.update",
            "session": sess,
        ])
    }

    /// Push fresh instructions (e.g. after remember/forget changed memory).
    private func refreshInstructions() async {
        if let profile = try? await ChatStore.shared.getPersonalMemory() {
            memoryProfile = profile
        }
        sendSessionUpdate()
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

    private func executeTool(name: String, argsJson: String, callId: String) {
        Task { @MainActor in
            self.onToolActive?(name)
        }
        Task {
            let result = await VoiceToolExecutor.shared.run(name: name, argsJson: argsJson)
            self.sendToolOutput(callId: callId, output: result.output)
            if let image = result.image {
                self.sendUserImage(dataUrl: image)
            }
            // remember/forget rewrite the stored profile; push it into the
            // live instructions to keep them in context.
            if name == "remember" || name == "forget" {
                await self.refreshInstructions()
            }
            self.requestResponse()
            Task { @MainActor in
                self.onToolDone?(name)
            }
        }
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

    private func requestResponse() {
        send([
            "type": "response.create",
            "response": [:] as [String: Any],
        ])
    }

    private func send(_ object: [String: Any]) {
        guard !closed,
              let ws = webSocket,
              JSONSerialization.isValidJSONObject(object),
              let data = try? JSONSerialization.data(withJSONObject: object),
              let text = String(data: data, encoding: .utf8)
        else { return }
        ws.send(.string(text)) { error in
            if let error {
                NSLog("[LiveVoice] send failed: \(error.localizedDescription)")
            }
        }
    }
}

/// Resample / PCM16 helpers. Called from the audio tap thread, so not MainActor.
private final class PCMBridge: @unchecked Sendable {
    private let playLock = NSLock()
    private var playConverter: AVAudioConverter?
    private var playOutRate: Double = 0
    private let micRate: Double = 16_000
    private let ttsRate: Double = 24_000

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
        let configured = UserDefaults.standard.object(forKey: "voice.micGain") as? Double ?? 0
        let gain: Float = configured > 0 ? Float(configured) : 3.0
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
