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

    init() {
        NotificationCenter.default.addObserver(
            self,
            selector: #selector(handleConfigChange),
            name: .AVAudioEngineConfigurationChange,
            object: engine
        )
    }

    deinit {
        watchdog?.invalidate()
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
        playbackLock.lock()
        activePlaybackBuffers = 0
        playbackLock.unlock()

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
        // Voice Processing IO (hardware AEC) defaults OFF: on this Mac's
        // built-in mic it engages but the tap turns 9-channel, and averaging
        // dilutes the mic ~10dB (borderline VAD, misheard words), while plain
        // input is a clean 1-channel tap with perfect transcripts. Speaker
        // echo is instead contained by software ducking + hangover in the tap
        // (see isPlaying). Opt in per-machine only after verifying levels:
        //   defaults write com.jack.Voice voice.enableVPIO -bool true
        // NOTE: `defaults` targets the sandbox container for open-.app
        // launches and the main domain for direct-binary runs; the in-code
        // default below is what keeps both paths consistent.
        let enableVPIO = (UserDefaults.standard.object(forKey: "voice.enableVPIO") as? Bool) ?? false

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
        if tapped {
            engine.inputNode.removeTap(onBus: 0)
            tapped = false
        }
        player.stop()
        playbackLock.lock()
        activePlaybackBuffers = 0
        playbackLock.unlock()
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
            if !self.engine.isRunning {
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

    private var activePlaybackBuffers = 0
    private let playbackLock = NSLock()
    private var lastPlayAt = Date.distantPast
    /// Speaker/room tail after the final buffer. Without hardware AEC the
    /// mic must stay ducked through this tail or our own reply loops back
    /// into the server VAD as a new user turn (echo loop).
    private let playbackHangover: TimeInterval = 0.9

    var isPlaying: Bool {
        playbackLock.lock()
        defer { playbackLock.unlock() }
        // No time-based reset: replies run 15s+ and every abandonment path
        // (stop/clearPlayback/restartEngine) zeroes the counter explicitly.
        // A time guard unducks the mic mid-reply and re-creates the echo loop.
        if activePlaybackBuffers > 0 { return true }
        return Date().timeIntervalSince(lastPlayAt) < playbackHangover
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
