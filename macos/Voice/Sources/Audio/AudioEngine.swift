import AVFoundation

/// Microphone capture and playback on one engine.
/// Taps the hardware input format and mixes to mono in the PCM path.
final class AudioEngine {

    var onInputLevel: ((Float) -> Void)?
    var onBuffer: ((AVAudioPCMBuffer) -> Void)?
    var onPlaybackDrained: (() -> Void)?
    private(set) var statusNote: String?
    private var headphones = false
    private let playback = PlaybackTracker()

    /// Created on first `start()`, after microphone permission. `stop()` must
    /// not create it — Voice tears down before start, and constructing
    /// AVAudioEngine while TCC is `.notDetermined` raises a sheet of its own.
    private var engine: AVAudioEngine?

    @discardableResult
    private func requireEngine() -> AVAudioEngine {
        if let engine { return engine }
        let created = AVAudioEngine()
        engine = created
        NotificationCenter.default.addObserver(
            self,
            selector: #selector(handleConfigChange),
            name: .AVAudioEngineConfigurationChange,
            object: created
        )
        return created
    }
    private let player = AVAudioPlayerNode()
    private var shouldBeRunning = false
    private var tapped = false
    private var muted = false
    private var lastLevelSent = Date.distantPast
    private(set) var voiceProcessingEnabled = false
    private var ioFormat: AVAudioFormat?
    private var watchdog: Timer?

    var playbackFormat: AVAudioFormat {
        ioFormat
            ?? AVAudioFormat(
                commonFormat: .pcmFormatFloat32,
                sampleRate: 48_000,
                channels: 1,
                interleaved: false
            )!
    }

    enum AudioError: LocalizedError {
        case microphoneDenied
        case engineFailed

        var errorDescription: String? {
            switch self {
            case .microphoneDenied: return "Microphone access is off"
            case .engineFailed: return "Couldn't start audio"
            }
        }
    }

    deinit {
        watchdog?.invalidate()
        NotificationCenter.default.removeObserver(self)
    }

    @objc private func handleConfigChange(_ note: Notification) {
        guard let engine else { return }
        NSLog("[AudioEngine] config changed; isRunning=\(engine.isRunning) shouldBeRunning=\(shouldBeRunning)")
        guard shouldBeRunning else { return }
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.1) { [weak self] in
            guard let self, self.shouldBeRunning, self.engine?.isRunning == false else { return }
            self.restartEngine()
        }
    }

    private func restartEngine() {
        guard shouldBeRunning, let engine, !engine.isRunning else { return }
        playback.clear()

        wireIO(format: nil)
        engine.prepare()
        do {
            try engine.start()
            if !player.isPlaying { player.play() }
            NSLog("[AudioEngine] restarted engine after config change; running=\(engine.isRunning)")
        } catch {
            NSLog("[AudioEngine] failed to restart engine after config change: \(error.localizedDescription)")
        }
    }

    private static var permissionTask: Task<Bool, Never>?

    /// One prompt, one API. Mixing `AVCaptureDevice.requestAccess` with
    /// `AVAudioEngine.start` shows two microphone sheets after a rebuild,
    /// when TCC is `.notDetermined` again.
    static func requestMicrophoneAccess() async -> Bool {
        if let permissionTask { return await permissionTask.value }
        let task = Task { await performMicrophoneRequest() }
        permissionTask = task
        let granted = await task.value
        permissionTask = nil
        return granted
    }

    private static func performMicrophoneRequest() async -> Bool {
        if #available(macOS 14.0, *) {
            switch AVAudioApplication.shared.recordPermission {
            case .granted: return true
            case .denied: return false
            default:
                return await withCheckedContinuation { continuation in
                    AVAudioApplication.requestRecordPermission { allowed in
                        continuation.resume(returning: allowed)
                    }
                }
            }
        }
        switch AVCaptureDevice.authorizationStatus(for: .audio) {
        case .authorized: return true
        case .notDetermined: return await AVCaptureDevice.requestAccess(for: .audio)
        default: return false
        }
    }

    func start() async throws {
        guard await AudioEngine.requestMicrophoneAccess() else {
            throw AudioError.microphoneDenied
        }

        let engine = requireEngine()
        if engine.isRunning { stop() }
        shouldBeRunning = true

        let input = engine.inputNode
        let mode = UserDefaults.standard.string(forKey: "voice.audioMode") ?? "automatic"
        headphones = mode == "headphones"
        let enableVPIO = mode == "automatic"
        statusNote = nil

        if enableVPIO {
            do {
                try input.setVoiceProcessingEnabled(true)
                voiceProcessingEnabled = true
                input.isVoiceProcessingAGCEnabled = true
            } catch {
                voiceProcessingEnabled = false
                try? input.setVoiceProcessingEnabled(false)
                NSLog("[AudioEngine] voice processing unavailable: \(error.localizedDescription)")
            }
        } else {
            voiceProcessingEnabled = false
            try? input.setVoiceProcessingEnabled(false)
        }

        wireIO(format: nil)
        engine.prepare()
        do {
            try engine.start()
        } catch {
            // Some input chains (e.g. this Mac's built-in mic/speakers)
            // accept the voice-processing flag but fail engine start with
            // -10875. Fall back to plain input + software ducking rather
            // than leaving the session dead.
            if voiceProcessingEnabled {
                NSLog("[AudioEngine] VPIO start failed (%@); retrying without voice processing", error.localizedDescription)
                voiceProcessingEnabled = false
                try? input.setVoiceProcessingEnabled(false)
                wireIO(format: nil)
                engine.prepare()
                do {
                    try engine.start()
                } catch {
                    NSLog("[AudioEngine] start failed: \(error.localizedDescription)")
                    shouldBeRunning = false
                    throw AudioError.engineFailed
                }
            } else {
                NSLog("[AudioEngine] start failed: \(error.localizedDescription)")
                shouldBeRunning = false
                throw AudioError.engineFailed
            }
        }

        if !voiceProcessingEnabled && !headphones {
            statusNote = "Speaker compatibility mode: use Stop reply to interrupt, or choose Headphones in Settings."
        }
        if !player.isPlaying { player.play() }
        let inFmt = engine.inputNode.inputFormat(forBus: 0)
        ioFormat = AVAudioFormat(
            commonFormat: .pcmFormatFloat32,
            sampleRate: inFmt.sampleRate > 0 ? inFmt.sampleRate : 48_000,
            channels: 1,
            interleaved: false
        ) ?? inFmt
        NSLog(
            "[AudioEngine] running rate=%.0f ch=%d vpe=%d",
            inFmt.sampleRate,
            inFmt.channelCount,
            voiceProcessingEnabled ? 1 : 0
        )
        armWatchdog()
    }

    func stop() {
        shouldBeRunning = false
        disarmWatchdog()
        guard let engine else {
            player.stop()
            playback.clear()
            ioFormat = nil
            DispatchQueue.main.async { [weak self] in self?.onInputLevel?(0) }
            return
        }
        if tapped {
            engine.inputNode.removeTap(onBus: 0)
            tapped = false
        }
        player.stop()
        playback.clear()
        engine.stop()
        ioFormat = nil
        DispatchQueue.main.async { [weak self] in self?.onInputLevel?(0) }
    }

    /// The engine can die silently (no configuration notice), leaving the tap
    /// dead while the session looks connected. Poll cheaply and restart.
    private func armWatchdog() {
        disarmWatchdog()
        watchdog = Timer.scheduledTimer(withTimeInterval: 2.0, repeats: true) { [weak self] _ in
            guard let self, self.shouldBeRunning else { return }
            if self.engine?.isRunning == false {
                NSLog("[AudioEngine] watchdog: engine stopped unexpectedly; restarting")
                self.restartEngine()
            }
        }
    }

    private func disarmWatchdog() {
        watchdog?.invalidate()
        watchdog = nil
    }

    func setMuted(_ muted: Bool) {
        self.muted = muted
        if muted { DispatchQueue.main.async { [weak self] in self?.onInputLevel?(0) } }
    }

    var isPlaying: Bool { playback.isAudible }

    func play(_ buffer: AVAudioPCMBuffer) {
        if !player.isPlaying { player.play() }
        let token = playback.enqueue()
        // dataPlayedBack tracks the speakers, not merely data consumed by the engine.
        player.scheduleBuffer(buffer, completionCallbackType: .dataPlayedBack) { [weak self] _ in
            guard let self else { return }
            if self.playback.complete(token) { self.onPlaybackDrained?() }
        }
    }

    func clearPlayback() {
        // Invalidate callbacks before stop() releases old scheduled buffers.
        playback.clear()
        player.stop()
        if engine?.isRunning == true { player.play() }
    }

    private func wireIO(format: AVAudioFormat?) {
        let engine = requireEngine()
        let input = engine.inputNode
        let hw = input.inputFormat(forBus: 0)
        let playFmt = format ?? AVAudioFormat(
            commonFormat: .pcmFormatFloat32,
            sampleRate: hw.sampleRate > 0 ? hw.sampleRate : 48_000,
            channels: 1,
            interleaved: false
        ) ?? hw
        if player.engine == nil {
            engine.attach(player)
        }
        engine.disconnectNodeOutput(player)
        engine.connect(player, to: engine.mainMixerNode, format: playFmt)
        // VPIO requires matching client-side capture and render formats.
        // Leaving the output at hardware stereo while tapping mono fails
        // initialization with -10875 on the built-in MacBook device pair.
        engine.disconnectNodeOutput(engine.mainMixerNode)
        engine.connect(engine.mainMixerNode, to: engine.outputNode,
                       format: voiceProcessingEnabled ? playFmt : nil)
        ioFormat = playFmt
        if tapped {
            input.removeTap(onBus: 0)
            tapped = false
        }
        // Ask the voice-processing output for mono, rather than averaging its
        // aggregate hardware channels (which diluted the processed microphone).
        let processed = input.outputFormat(forBus: 0)
        let tapFormat = voiceProcessingEnabled ? AVAudioFormat(
            commonFormat: .pcmFormatFloat32, sampleRate: processed.sampleRate,
            channels: 1, interleaved: false
        ) : nil
        input.installTap(onBus: 0, bufferSize: 1024, format: tapFormat) { [weak self] buffer, _ in
            guard let self, !self.muted else { return }
            // If hardware AEC is unavailable and audio is actively playing through speakers,
            // duck the tap buffer so speaker audio cannot loop back to the server VAD.
            if !self.voiceProcessingEnabled && !self.headphones && self.playback.needsEchoGuard() {
                DispatchQueue.main.async { [weak self] in self?.onInputLevel?(0) }
                return
            }
            self.onBuffer?(buffer)
            self.publishLevel(from: buffer)
        }
        tapped = true
    }

    private func publishLevel(from buffer: AVAudioPCMBuffer) {
        let now = Date()
        guard now.timeIntervalSince(lastLevelSent) > 1.0 / 30 else { return }
        lastLevelSent = now
        guard let channels = buffer.floatChannelData else { return }
        let count = Int(buffer.frameLength)
        let chCount = Int(buffer.format.channelCount)
        guard count > 0, chCount > 0 else { return }
        var sum: Float = 0
        for i in 0..<count {
            var mixed: Float = 0
            for c in 0..<chCount { mixed += channels[c][i] }
            mixed /= Float(chCount)
            sum += mixed * mixed
        }
        let rms = (sum / Float(count)).squareRoot()
        let db = 20 * log10(max(rms, 0.000_001))
        let level = max(0, min(1, (db + 50) / 50))
        DispatchQueue.main.async { [weak self] in self?.onInputLevel?(level) }
    }
}
