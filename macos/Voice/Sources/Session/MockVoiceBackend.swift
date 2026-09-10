import Foundation

/// Replays a scripted conversation on a timer, with fake audio levels.
///
/// Build the entire interface against this before wiring up any real service:
/// every state in SPEC.md section 6 is reachable on demand, which is not true
/// once a network is involved. Keep it after you ship — it is how you test the
/// states you cannot reproduce when you want them.
@MainActor
final class MockVoiceBackend: VoiceBackend {

    var onState: ((SessionState) -> Void)?
    var onInputLevel: ((Float) -> Void)?
    var onOutputLevel: ((Float) -> Void)?
    var onUserSpeechStarted: (() -> Void)?
    var onTurnDropped: (() -> Void)?
    var onUserPartial: ((String, String?) -> Void)?
    var onUserFinal: ((String, String?) -> Void)?
    var onAgentDelta: ((String) -> Void)?
    var onAgentDone: (() -> Void)?
    var onToolActive: ((String) -> Void)?
    var onToolDone: ((String, String) -> Void)?
    var onAudioStatus: ((String?) -> Void)?
    var onToolsCancelled: (() -> Void)?

    private struct Exchange {
        let said: String
        let reply: String
    }

    private let script: [Exchange] = [
        Exchange(said: "What's left on my list today?",
                 reply: "Two things — the vendor call at 3:30, and the freezer count."),
        Exchange(said: "Move the vendor call to four.",
                 reply: "Done, it's at 4:00 now. Want me to let Dana know?")
    ]

    private var running: Task<Void, Never>?
    private var levels: Task<Void, Never>?
    private var muted = false

    func start() async throws {
        onState?(.connecting)
        try? await Task.sleep(nanoseconds: 700_000_000)
        startLevels()
        running = Task { await self.play() }
    }

    func stop() async {
        running?.cancel(); running = nil
        levels?.cancel(); levels = nil
        onInputLevel?(0)
        onOutputLevel?(0)
        onState?(.idle)
    }

    func setMuted(_ muted: Bool) {
        self.muted = muted
        if muted { onInputLevel?(0) }
    }

    func interrupt() {
        running?.cancel()
        onAgentDone?()
        onState?(.listening)
    }

    // MARK: - Playback

    private func play() async {
        for exchange in script {
            if Task.isCancelled { return }
            onState?(.listening)

            // Type the user's line out word by word as an interim transcript.
            var partial = ""
            for word in exchange.said.split(separator: " ") {
                if Task.isCancelled { return }
                partial += (partial.isEmpty ? "" : " ") + word
                onUserPartial?(partial, nil)
                try? await Task.sleep(nanoseconds: 160_000_000)
            }
            onUserFinal?(exchange.said, nil)

            if Task.isCancelled { return }
            onState?(.agentSpeaking)
            try? await Task.sleep(nanoseconds: 400_000_000)

            for word in exchange.reply.split(separator: " ") {
                if Task.isCancelled { return }
                onAgentDelta?((exchange.reply.hasPrefix(String(word)) ? "" : " ") + word)
                try? await Task.sleep(nanoseconds: 120_000_000)
            }
            onAgentDone?()
            try? await Task.sleep(nanoseconds: 800_000_000)
        }

        if !Task.isCancelled { onState?(.listening) }
    }

    /// Plausible-looking levels so the meter and the orb ring can be judged.
    private func startLevels() {
        levels = Task {
            var phase: Float = 0
            while !Task.isCancelled {
                phase += 0.28
                let wave = (sin(phase) + 1) / 2
                let jitter = Float.random(in: 0...0.35)
                let level = min(1, wave * 0.6 + jitter)
                if !self.muted { self.onInputLevel?(level) }
                self.onOutputLevel?(min(1, wave * 0.5 + Float.random(in: 0...0.3)))
                try? await Task.sleep(nanoseconds: 33_000_000)   // ~30 Hz
            }
        }
    }
}
