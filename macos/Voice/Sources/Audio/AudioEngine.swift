import AVFoundation

/// Microphone capture and playback on one engine.
/// Taps the hardware input format and mixes to mono in the PCM path.
final class AudioEngine {

    var onInputLevel: ((Float) -> Void)?
    var onBuffer: ((AVAudioPCMBuffer) -> Void)?

    private let engine = AVAudioEngine()
    private let player = AVAudioPlayerNode()
    private var shouldBeRunning = false
    private var tapped = false
    private var muted = false
    private var lastLevelSent = Date.distantPast
    private(set) var voiceProcessingEnabled = false
    private var ioFormat: AVAudioFormat?

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

    init() {
        NotificationCenter.default.addObserver(
            self,
            selector: #selector(handleConfigChange),
            name: .AVAudioEngineConfigurationChange,
            object: engine
        )
    }

    deinit {
        NotificationCenter.default.removeObserver(self)
    }

    @objc private func handleConfigChange(_ note: Notification) {
        NSLog("[AudioEngine] config changed; isRunning=\(engine.isRunning) shouldBeRunning=\(shouldBeRunning)")
        guard shouldBeRunning else { return }
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.1) { [weak self] in
            guard let self, self.shouldBeRunning, !self.engine.isRunning else { return }
            self.restartEngine()
        }
    }

    private func restartEngine() {
        guard shouldBeRunning, !engine.isRunning else { return }

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

    static func requestMicrophoneAccess() async -> Bool {
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

        if engine.isRunning { stop() }
        shouldBeRunning = true

        let input = engine.inputNode
        let inChannels = input.inputFormat(forBus: 0).channelCount
        // Voice Processing IO (hardware AEC) defaults ON: without it the
        // speaker output re-enters the mic and the server transcribes our own
        // replies as new user turns (echo loop). Disable only deliberately:
        //   defaults write com.jack.Voice voice.enableVPIO -bool false
        // The flag lives in the app container when opened as .app and in the
        // main domain for a direct binary launch; defaulting to true keeps
        // both working. Unsupported hardware falls back below to no-VPIO.
        let enableVPIO = (UserDefaults.standard.object(forKey: "voice.enableVPIO") as? Bool) ?? true

        if enableVPIO && inChannels <= 2 {
            do {
                try input.setVoiceProcessingEnabled(true)
                voiceProcessingEnabled = true
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
            NSLog("[AudioEngine] start failed: \(error.localizedDescription)")
            shouldBeRunning = false
            throw AudioError.engineFailed
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
    }

    func stop() {
        shouldBeRunning = false
        if tapped {
            engine.inputNode.removeTap(onBus: 0)
            tapped = false
        }
        player.stop()
        activePlaybackBuffers = 0
        engine.stop()
        ioFormat = nil
        DispatchQueue.main.async { [weak self] in self?.onInputLevel?(0) }
    }

    func setMuted(_ muted: Bool) {
        self.muted = muted
        if muted { DispatchQueue.main.async { [weak self] in self?.onInputLevel?(0) } }
    }

    private var activePlaybackBuffers = 0
    private let playbackLock = NSLock()
    private var lastPlayAt = Date.distantPast

    var isPlaying: Bool {
        playbackLock.lock()
        let count = activePlaybackBuffers
        let sincePlay = Date().timeIntervalSince(lastPlayAt)
        playbackLock.unlock()
        // Completion blocks can be lost on engine restart/stop, which would
        // otherwise stick the counter >0 forever and permanently duck the mic
        // (second utterance goes silent while levels still animate).
        // Buffers are short; anything older than 2s is stale.
        guard count > 0 else { return false }
        if sincePlay > 2.0 {
            playbackLock.lock()
            // Re-check under lock before clearing a potentially fresh play().
            if Date().timeIntervalSince(lastPlayAt) > 2.0 {
                activePlaybackBuffers = 0
            }
            let fresh = activePlaybackBuffers > 0
            playbackLock.unlock()
            return fresh
        }
        return true
    }

    func play(_ buffer: AVAudioPCMBuffer) {
        if !player.isPlaying { player.play() }
        playbackLock.lock()
        activePlaybackBuffers += 1
        lastPlayAt = Date()
        playbackLock.unlock()
        player.scheduleBuffer(buffer) { [weak self] in
            guard let self else { return }
            self.playbackLock.lock()
            self.activePlaybackBuffers = max(0, self.activePlaybackBuffers - 1)
            self.playbackLock.unlock()
        }
    }

    func clearPlayback() {
        player.stop()
        playbackLock.lock()
        activePlaybackBuffers = 0
        playbackLock.unlock()
        if engine.isRunning { player.play() }
    }

    private func wireIO(format: AVAudioFormat?) {
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
        ioFormat = playFmt
        if tapped {
            input.removeTap(onBus: 0)
            tapped = false
        }
        // nil format = hardware layout. Mixing to mono in the
        // PCM path avoids silent taps from forcing a mono installTap format.
        input.installTap(onBus: 0, bufferSize: 1024, format: nil) { [weak self] buffer, _ in
            guard let self, !self.muted else { return }
            // If hardware AEC is unavailable and audio is actively playing through speakers,
            // duck the tap buffer so speaker audio cannot loop back to the server VAD.
            if !self.voiceProcessingEnabled && self.isPlaying {
                self.publishLevel(from: buffer)
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
