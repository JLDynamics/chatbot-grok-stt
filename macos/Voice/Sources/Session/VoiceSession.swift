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
    var onUserFinal: ((String, String?) -> Void)? { get set } // final transcript + server item id (stable across pause-merged segments)
    var onAgentDelta: ((String) -> Void)? { get set }       // streamed reply text
    var onAgentDone: (() -> Void)? { get set }
    var onToolActive: ((String) -> Void)? { get set }
    var onToolDone: ((String) -> Void)? { get set }

    func start() async throws
    func stop() async
    func setMuted(_ muted: Bool)
    func interrupt()
    /// Saved transcript to replay into the live conversation once the server
    /// acknowledges the session (mirrors the web `_replayHistory`, last 20).
    /// Each entry is (role, text) with role in user/assistant/tool.
    func setHistory(_ messages: [(role: String, text: String, name: String?)])
    /// Re-read the stored memory profile into live instructions.
    func refreshMemory() async
}

extension VoiceBackend {
    func setHistory(_ messages: [(role: String, text: String, name: String?)]) {}
    func refreshMemory() async {}
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

    // MARK: - Saved conversations + memory (mirrors web_app/main.js)

    @Published private(set) var sessions: [ChatSessionSummary] = []
    @Published private(set) var personalMemory = ""
    @Published private(set) var activeSessionTitle = "New conversation"
    @Published var showSessions = false

    var currentSessionId: String? { activeSessionId }

    private var activeSessionId: String?
    private var messages: [ChatMessage] = []
    private var pendingAgentText = ""
    private var lastUserItemId: String?
    private var saveTask: Task<Void, Never>?
    private var dirty = false
    private static let lastSessionKey = "chat.sessionId"

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
        Task { [weak self] in
            await self?.loadMemory()
            await self?.refreshSessions()
            await self?.restoreLastSession()
        }
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

        backend.onUserFinal = { [weak self] text, itemId in
            guard let self else { return }
            self.interim = nil
            self.upsertUserTurn(text: text, itemId: itemId)
        }

        // Deltas stream into a single agent turn rather than appending rows.
        backend.onAgentDelta = { [weak self] chunk in
            guard let self else { return }
            self.pendingAgentText += chunk
            if let last = self.turns.last, last.speaker == .agent, self.agentTurnOpen {
                self.turns[self.turns.count - 1].text += chunk
            } else {
                self.agentTurnOpen = true
                self.append(Turn(speaker: .agent, text: chunk))
            }
        }
        backend.onAgentDone = { [weak self] in
            guard let self else { return }
            self.agentTurnOpen = false
            let text = self.pendingAgentText.trimmingCharacters(in: .whitespacesAndNewlines)
            self.pendingAgentText = ""
            if !text.isEmpty { self.recordAssistant(text) }
        }

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
        // Hand the saved transcript to the backend so the live session opens
        // with the same context as the web client.
        backend.setHistory(messages.map { ($0.role, $0.text, $0.name) })
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
        // Keep a cut-off reply in history like the web "[interrupted]" marker.
        let leftover = pendingAgentText.trimmingCharacters(in: .whitespacesAndNewlines)
        pendingAgentText = ""
        if !leftover.isEmpty { recordAssistant(leftover + " [interrupted]") }
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
            await flushSave()
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
        await flushSave()
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

    // MARK: - Saved conversations + memory

    func refreshSessions() async {
        do {
            sessions = try await ChatStore.shared.listSessions()
        } catch {
            NSLog("[Voice] could not list chat sessions: \(error.localizedDescription)")
        }
    }

    func newSession() async {
        await flushSave()
        resetTranscript()
        UserDefaults.standard.removeObject(forKey: Self.lastSessionKey)
    }

    func openSession(id: String) async {
        if id == activeSessionId { return }
        await flushSave()
        do {
            let s = try await ChatStore.shared.getSession(id: id)
            dirty = false
            activeSessionId = s.id
            activeSessionTitle = s.title
            messages = s.messages
            turns = s.messages.compactMap { m -> Turn? in
                switch m.role {
                case "user": return Turn(speaker: .you, text: m.text)
                case "assistant": return Turn(speaker: .agent, text: m.text)
                default: return nil
                }
            }
            interim = nil
            agentTurnOpen = false
            pendingAgentText = ""
            UserDefaults.standard.set(s.id, forKey: Self.lastSessionKey)
        } catch {
            NSLog("[Voice] could not open chat session: \(error.localizedDescription)")
        }
    }

    func deleteSession(id: String) async {
        if id == activeSessionId {
            saveTask?.cancel()
            saveTask = nil
            resetTranscript()
            UserDefaults.standard.removeObject(forKey: Self.lastSessionKey)
        }
        do {
            try await ChatStore.shared.deleteSession(id: id)
        } catch {
            NSLog("[Voice] could not delete chat session: \(error.localizedDescription)")
        }
        await refreshSessions()
    }

    func loadMemory() async {
        do {
            personalMemory = try await ChatStore.shared.getPersonalMemory()
        } catch {
            NSLog("[Voice] could not load personal memory")
        }
    }

    /// Returns false when the save failed (e.g. profile too long).
    func saveMemory(_ text: String) async -> Bool {
        do {
            personalMemory = try await ChatStore.shared.putPersonalMemory(content: text)
            await backend.refreshMemory()
            return true
        } catch {
            NSLog("[Voice] could not save personal memory: \(error.localizedDescription)")
            return false
        }
    }

    private func resetTranscript() {
        dirty = false
        activeSessionId = nil
        activeSessionTitle = "New conversation"
        messages = []
        turns = []
        interim = nil
        agentTurnOpen = false
        pendingAgentText = ""
        lastUserItemId = nil
    }

    private func restoreLastSession() async {
        guard let id = UserDefaults.standard.string(forKey: Self.lastSessionKey),
              !id.isEmpty
        else { return }
        await openSession(id: id)
    }

    private func recordUser(_ text: String) {
        let t = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !t.isEmpty else { return }
        messages.append(ChatMessage(role: "user", text: t))
        if activeSessionTitle == "New conversation" {
            let flat = t.replacingOccurrences(of: "\\s+", with: " ", options: .regularExpression)
            activeSessionTitle = String(flat.prefix(72))
        }
        dirty = true
        scheduleSave()
    }

    /// Merge pause-split finals: the server reuses one item id across
    /// reopened segments of a single turn, so a matching id updates the
    /// existing bubble instead of appending a duplicate.
    private func upsertUserTurn(text: String, itemId: String?) {
        let t = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !t.isEmpty else { return }
        if let itemId, itemId == lastUserItemId,
           let last = turns.last, last.speaker == .you {
            turns[turns.count - 1].text = t
            if let idx = messages.lastIndex(where: { $0.role == "user" }) {
                messages[idx].text = t
            }
            dirty = true
            scheduleSave()
            return
        }
        lastUserItemId = itemId
        append(Turn(speaker: .you, text: t))
        recordUser(t)
    }

    private func recordAssistant(_ text: String) {
        let t = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !t.isEmpty else { return }
        messages.append(ChatMessage(role: "assistant", text: t))
        dirty = true
        scheduleSave()
    }

    private func scheduleSave() {
        saveTask?.cancel()
        saveTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: 500_000_000)
            guard !Task.isCancelled else { return }
            await self?.flushSave()
        }
    }

    func flushSave() async {
        saveTask?.cancel()
        saveTask = nil
        guard dirty, !messages.isEmpty else { return }
        guard let id = await ensureSession() else { return }
        do {
            try await ChatStore.shared.patchSession(id: id, title: activeSessionTitle, messages: messages)
            dirty = false
        } catch {
            NSLog("[Voice] could not save chat session: \(error.localizedDescription)")
        }
    }

    private func ensureSession() async -> String? {
        if let id = activeSessionId { return id }
        do {
            let created = try await ChatStore.shared.createSession(title: activeSessionTitle)
            activeSessionId = created.id
            UserDefaults.standard.set(created.id, forKey: Self.lastSessionKey)
            await refreshSessions()
            return created.id
        } catch {
            NSLog("[Voice] could not create chat session: \(error.localizedDescription)")
            return nil
        }
    }
}
