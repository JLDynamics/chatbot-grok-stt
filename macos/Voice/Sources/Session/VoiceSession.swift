import Foundation

enum SessionState: Equatable {
    case idle
    case connecting
    case listening
    case agentSpeaking
    case failed(String)
}

struct Turn: Identifiable, Equatable {
    enum Speaker { case you, agent }

    let id = UUID()
    let speaker: Speaker
    var text: String
    let at: Date

    init(speaker: Speaker, text: String, at: Date = Date()) {
        self.speaker = speaker
        self.text = text
        self.at = at
    }
}

/// The swappable half. Implement this against whatever realtime voice service you
/// choose; nothing above this line touches a network. Build the interface against
/// MockVoiceBackend first — see README.
@MainActor
protocol VoiceBackend: AnyObject {
    var onState: ((SessionState) -> Void)? { get set }
    var onInputLevel: ((Float) -> Void)? { get set }        // 0...1, ~30 Hz
    var onOutputLevel: ((Float) -> Void)? { get set }       // 0...1, ~30 Hz
    var onUserPartial: ((String) -> Void)? { get set }      // interim transcript
    var onUserFinal: ((String) -> Void)? { get set }
    var onAgentDelta: ((String) -> Void)? { get set }       // streamed reply text
    var onAgentDone: (() -> Void)? { get set }
    var onToolActive: ((String) -> Void)? { get set }
    var onToolDone: ((String) -> Void)? { get set }

    func start() async throws
    func stop() async
    func setMuted(_ muted: Bool)
    func interrupt()
}

/// What the interface observes. Owns the transcript and the visible state; the
/// backend only feeds it events.
@MainActor
final class SessionController: ObservableObject {

    @Published private(set) var state: SessionState = .idle
    @Published private(set) var turns: [Turn] = []
    @Published private(set) var interim: String?
    @Published private(set) var activeTool: String?
    @Published private(set) var inputLevel: Float = 0
    @Published private(set) var outputLevel: Float = 0
    @Published private(set) var isMuted = false
    @Published var showSettings = false

    /// Shown as one line in the header. Cleared on the next successful start.
    @Published private(set) var errorText: String?

    var isLive: Bool {
        switch state {
        case .idle, .failed: return false
        case .connecting, .listening, .agentSpeaking: return true
        }
    }

    private let backend: VoiceBackend
    private let maxTurns = 200

    init(backend: VoiceBackend) {
        self.backend = backend
        wire()
    }

    private func wire() {
        backend.onState = { [weak self] state in
            guard let self else { return }
            self.state = state
            if case .failed(let message) = state { self.errorText = message }
        }
        backend.onInputLevel = { [weak self] level in self?.inputLevel = level }
        backend.onOutputLevel = { [weak self] level in self?.outputLevel = level }

        backend.onUserPartial = { [weak self] text in self?.interim = text }

        backend.onUserFinal = { [weak self] text in
            guard let self else { return }
            self.interim = nil
            self.append(Turn(speaker: .you, text: text))
        }

        // Deltas stream into a single agent turn rather than appending rows.
        backend.onAgentDelta = { [weak self] chunk in
            guard let self else { return }
            if let last = self.turns.last, last.speaker == .agent, self.agentTurnOpen {
                self.turns[self.turns.count - 1].text += chunk
            } else {
                self.agentTurnOpen = true
                self.append(Turn(speaker: .agent, text: chunk))
            }
        }
        backend.onAgentDone = { [weak self] in self?.agentTurnOpen = false }

        backend.onToolActive = { [weak self] name in
            let desc: String
            switch name {
            case "web_search": desc = "Searching the web…"
            case "web_fetch": desc = "Fetching webpage…"
            case "read_article": desc = "Reading Chrome article…"
            case "control_screen": desc = "Controlling desktop…"
            case "code_agent": desc = "Coding agent running…"
            case "inspect_current_context": desc = "Checking context…"
            case "remember": desc = "Saving memory…"
            default: desc = "Running \(name)…"
            }
            self?.activeTool = desc
        }
        backend.onToolDone = { [weak self] _ in
            self?.activeTool = nil
        }
    }

    private var agentTurnOpen = false

    private func append(_ turn: Turn) {
        turns.append(turn)
        if turns.count > maxTurns { turns.removeFirst(turns.count - maxTurns) }
    }

    // MARK: - Intents

    private var isTransitioning = false
    private var beginTask: Task<Void, Never>?

    func toggleSession() {
        // Allow tapping End while a connect is still in flight;
        // otherwise the orb looks dead for up to the 8s handshake timeout.
        if isLive, state == .connecting {
            beginTask?.cancel()
            Task { await end() }
            return
        }
        guard !isTransitioning else { return }
        Task {
            isLive ? await end() : await begin()
        }
    }

    func begin() async {
        guard !isTransitioning else { return }
        isTransitioning = true
        defer { isTransitioning = false }
        errorText = nil
        beginTask?.cancel()
        // Capture the task so a tap-to-cancel during connect can interrupt it.
        let task = Task { @MainActor in
            do {
                try await backend.start()
            } catch is CancellationError {
                // Cancelled via toggle during connecting; backend.stop() in end() cleans up.
            } catch {
                state = .failed(error.localizedDescription)
                errorText = error.localizedDescription
            }
        }
        beginTask = task
        await task.value
        beginTask = nil
        if task.isCancelled {
            // Tap-to-cancel won during connecting but backend.start() still
            // ran to completion underneath: shut it back down so we never
            // leave a live mic/WS behind an idle UI.
            await backend.stop()
            interim = nil
            agentTurnOpen = false
            if isMuted {
                isMuted = false
                backend.setMuted(false)
            }
            inputLevel = 0
            outputLevel = 0
            state = .idle
        }
    }

    func end() async {
        beginTask?.cancel()
        beginTask = nil
        guard !isTransitioning else {
            // If a begin is in flight, stop the backend anyway so we never
            // get stuck in .connecting with a live connection.
            await backend.stop()
            interim = nil
            agentTurnOpen = false
            if isMuted {
                isMuted = false
                backend.setMuted(false)
            }
            inputLevel = 0
            outputLevel = 0
            state = .idle
            return
        }
        isTransitioning = true
        defer { isTransitioning = false }
        await backend.stop()
        interim = nil
        agentTurnOpen = false
        if isMuted {
            isMuted = false
            backend.setMuted(false)
        }
        inputLevel = 0
        outputLevel = 0
        state = .idle
    }

    func toggleMute() {
        guard isLive else { return }
        isMuted.toggle()
        backend.setMuted(isMuted)
        if isMuted { inputLevel = 0 }
    }

    /// Only wired up if Stop is a barge-in rather than an end — see SPEC.md §7.
    func interrupt() {
        guard case .agentSpeaking = state else { return }
        backend.interrupt()
    }
}
