import Foundation
import Combine

enum SessionState: Equatable {
    case idle
    case connecting
    case listening
    case agentSpeaking
    case failed(String)
}

/// Mic/speaker meters update ~30 Hz. Keep them off SessionController so the
/// rest of the panel (buttons, settings, transcript) does not rebuild every tick.
@MainActor
final class AudioLevels: ObservableObject {
    @Published var input: Float = 0
    @Published var output: Float = 0

    func reset() {
        input = 0
        output = 0
    }
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
    var onUserPartial: ((String, String?) -> Void)? { get set }      // interim transcript
    var onUserFinal: ((String, String?) -> Void)? { get set } // final transcript + server item id (stable across pause-merged segments)
    var onAgentDelta: ((String) -> Void)? { get set }       // streamed reply text
    var onAgentDone: (() -> Void)? { get set }
    var onToolActive: ((String) -> Void)? { get set }
    /// (tool name, short result summary) — the summary is recorded in the
    /// transcript so later turns can see what a tool actually returned.
    var onToolDone: ((String, String) -> Void)? { get set }
    var onAudioStatus: ((String?) -> Void)? { get set }
    var onToolsCancelled: (() -> Void)? { get set }

    func start() async throws
    func stop() async
    func setMuted(_ muted: Bool)
    func interrupt()
    /// Saved transcript to replay into the live conversation once the server
    /// acknowledges the session (mirrors the web `_replayHistory`, last 20).
    /// Each entry is (role, text) with role in user/assistant/tool.
    func setHistory(_ messages: [(role: String, text: String, name: String?)])
    /// Push the current Settings tool toggles into a live session.
    func refreshTools()
}

extension VoiceBackend {
    func setHistory(_ messages: [(role: String, text: String, name: String?)]) {}
    func refreshTools() {}
}

/// What the interface observes. Owns the transcript and the visible state; the
/// backend only feeds it events.
@MainActor
final class SessionController: ObservableObject {

    @Published private(set) var state: SessionState = .idle
    @Published private(set) var turns: [Turn] = []
    @Published private(set) var interim: String?
    @Published private(set) var activeTool: String?
    let levels = AudioLevels()
    @Published private(set) var isMuted = false
    @Published var showSettings = false

    /// Shown as one line in the header. Cleared on the next successful start.
    @Published private(set) var errorText: String?
    @Published private(set) var audioStatus: String?
    @Published private(set) var interimItemId: String?
    private var userRows: [String: UUID] = [:]
    private var userMessages: [String: Int] = [:]

    // MARK: - Saved conversations + memory (via ChatStore against the sidecar /api)

    @Published private(set) var sessions: [ChatSessionSummary] = []
    @Published private(set) var personalMemory = ""
    @Published private(set) var memoryError: String?
    @Published private(set) var sessionsError: String?
    @Published private(set) var sidecarConfig: SidecarConfig?
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

    init(backend: VoiceBackend, restoreSavedSession: Bool = true) {
        self.backend = backend
        wire()
        guard restoreSavedSession else { return }
        Task { [weak self] in
            await self?.loadMemory()
            await self?.refreshSessions()
            await self?.refreshConfig()
            await self?.restoreLastSession()
        }
    }

    private func wire() {
        backend.onState = { [weak self] state in
            guard let self else { return }
            self.state = state
            if case .failed(let message) = state { self.errorText = message }
        }
        backend.onInputLevel = { [weak self] level in self?.levels.input = level }
        backend.onOutputLevel = { [weak self] level in self?.levels.output = level }

        backend.onAudioStatus = { [weak self] note in self?.audioStatus = note }
        backend.onToolsCancelled = { [weak self] in self?.activeTool = nil }
        backend.onUserPartial = { [weak self] text, itemId in
            self?.interimItemId = itemId
            self?.interim = text
        }

        backend.onUserFinal = { [weak self] text, itemId in
            guard let self else { return }
            self.interim = nil
            self.interimItemId = nil
            self.upsertUserTurn(text: text, itemId: itemId)
        }

        // Deltas stream into a single agent turn rather than appending rows.
        backend.onAgentDelta = { [weak self] chunk in
            guard let self else { return }
            self.userTurnOpen = false
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
            self.userTurnOpen = false
            let text = self.pendingAgentText.trimmingCharacters(in: .whitespacesAndNewlines)
            self.pendingAgentText = ""
            if !text.isEmpty { self.recordAssistant(text) }
        }
        backend.onToolActive = { [weak self] name in
            self?.userTurnOpen = false
            let desc: String
            switch name {
            case "web_search": desc = "Searching the web…"
            case "read_page": desc = "Reading page…"
            case "screenshot": desc = "Taking a screenshot…"
            case "code_agent": desc = "Coding agent running…"
            case "remember": desc = "Saving memory…"
            case "forget": desc = "Updating memory…"
            case "search_chat_history": desc = "Searching past chats…"
            default: desc = "Running \(name)…"
            }
            self?.activeTool = desc
        }
        backend.onToolDone = { [weak self] name, summary in
            self?.activeTool = nil
            // replayHistory already renders a "tool" turn as "[Earlier I used
            // X] ..." but nothing ever wrote one, so the model's own history
            // showed it narrating actions with no record of the result. That
            // gap is what let it describe work it had not managed to do.
            self?.recordTool(name: name, summary: summary)
        }
    }

    private var agentTurnOpen = false
    /// True while the last visible turn is the user's still-open turn: later
    /// finals for the same spoken stretch (pause-split revisions, even with a
    /// fresh item id) extend it instead of appending duplicate bubbles.
    /// Cleared by any assistant/agent/tool activity or session change.
    private var userTurnOpen = false
    private func append(_ turn: Turn) {
        if turn.speaker != .you { userTurnOpen = false }
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
        if isLive {
            Task { await end() }
            return
        }
        errorText = nil
        state = .connecting
        Task { await begin() }
    }

    func begin() async {
        guard !isTransitioning else { return }
        userTurnOpen = false
        isTransitioning = true
        errorText = nil
        if state != .connecting { state = .connecting }
        beginTask?.cancel()
        // Hand the saved transcript to the backend so the live session opens
        // with the saved context.
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
        let cancelled = task.isCancelled
        isTransitioning = false
        if cancelled {
            // Tap-to-cancel won during connecting but backend.start() still
            // ran to completion underneath: shut it back down so we never
            // leave a live mic/WS behind an idle UI.
            await backend.stop()
            interim = nil
            agentTurnOpen = false
            userTurnOpen = false
            if isMuted {
                isMuted = false
                backend.setMuted(false)
            }
            levels.reset()
            state = .idle
            return
        }
        if errorText == nil {
            await loadMemory()
            await refreshSessions()
            await refreshConfig()
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
            userTurnOpen = false
            if isMuted {
                isMuted = false
                backend.setMuted(false)
            }
            levels.reset()
            state = .idle
            await flushSave()
            return
        }
        isTransitioning = true
        defer { isTransitioning = false }
        await backend.stop()
        interim = nil
        agentTurnOpen = false
        userTurnOpen = false
        if isMuted {
            isMuted = false
            backend.setMuted(false)
        }
        levels.reset()
        state = .idle
        activeTool = nil
        await flushSave()
    }

    func toggleMute() {
        guard isLive else { return }
        isMuted.toggle()
        backend.setMuted(isMuted)
        if isMuted { levels.input = 0 }
    }

    func applyToolSettings() {
        backend.refreshTools()
    }

    /// Only wired up if Stop is a barge-in rather than an end — see SPEC.md §7.
    func interrupt() {
        guard isLive, state != .connecting else { return }
        backend.interrupt()
        activeTool = nil
    }

    // MARK: - Saved conversations + memory

    func refreshSessions() async {
        do {
            try await LocalServiceStarter.shared.ensureSidecar()
            sessions = try await ChatStore.shared.listSessions()
            sessionsError = nil
        } catch {
            sessionsError = error.localizedDescription
            NSLog("[Voice] could not list chat sessions: \(error.localizedDescription)")
        }
    }

    func newSession() async {
        if isLive { await end() }
        await flushSave()
        resetTranscript()
        UserDefaults.standard.removeObject(forKey: Self.lastSessionKey)
    }

    func openSession(id: String) async {
        if id == activeSessionId { return }
        if isLive { await end() }
        await flushSave()
        do {
            try await LocalServiceStarter.shared.ensureSidecar()
            let s = try await ChatStore.shared.getSession(id: id)
            dirty = false
            activeSessionId = s.id
            activeSessionTitle = s.title
            userRows.removeAll()
            userMessages.removeAll()
            lastUserItemId = nil
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
            userTurnOpen = false
            pendingAgentText = ""
            UserDefaults.standard.set(s.id, forKey: Self.lastSessionKey)
        } catch {
            NSLog("[Voice] could not open chat session: \(error.localizedDescription)")
        }
    }

    func deleteSession(id: String) async {
        if id == activeSessionId {
            if isLive { await end() }
            saveTask?.cancel()
            saveTask = nil
            resetTranscript()
            UserDefaults.standard.removeObject(forKey: Self.lastSessionKey)
        }
        do {
            try await LocalServiceStarter.shared.ensureSidecar()
            try await ChatStore.shared.deleteSession(id: id)
            sessionsError = nil
        } catch {
            sessionsError = error.localizedDescription
            NSLog("[Voice] could not delete chat session: \(error.localizedDescription)")
        }
        await refreshSessions()
    }

    func loadMemory() async {
        do {
            try await LocalServiceStarter.shared.ensureSidecar()
            personalMemory = try await ChatStore.shared.getPersonalMemory()
            memoryError = nil
        } catch {
            memoryError = error.localizedDescription
            NSLog("[Voice] could not load personal memory: \(error.localizedDescription)")
        }
    }

    func refreshConfig() async {
        do {
            sidecarConfig = try await ChatStore.shared.getConfig()
        } catch {
            sidecarConfig = nil
            NSLog("[Voice] could not load sidecar config: \(error.localizedDescription)")
        }
    }

    /// Returns false when the save failed (e.g. profile too long).
    func saveMemory(_ text: String) async -> Bool {
        do {
            try await LocalServiceStarter.shared.ensureSidecar()
            personalMemory = try await ChatStore.shared.putPersonalMemory(content: text)
            memoryError = nil
            // The server re-reads the profile from the sidecar on the next turn.
            return true
        } catch {
            memoryError = error.localizedDescription
            NSLog("[Voice] could not save personal memory: \(error.localizedDescription)")
            return false
        }
    }

    private func resetTranscript() {
        userRows.removeAll()
        userMessages.removeAll()
        dirty = false
        activeSessionId = nil
        activeSessionTitle = "New conversation"
        messages = []
        turns = []
        interim = nil
        agentTurnOpen = false
        pendingAgentText = ""
        lastUserItemId = nil
        userTurnOpen = false
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

    /// Merge pause-split finals: the server usually reuses one item id across
    /// reopened segments of a single turn, so a matching id updates the
    /// existing bubble instead of appending a duplicate. The open-turn flag
    /// covers the rest: while no assistant reply intervened, a later final
    /// extends the same bubble even with a fresh id. The continuation rule
    /// below covers the last gap: the assistant answered the partial before
    /// you finished, so the restated final extends an earlier bubble with
    /// other rows in between. One utterance still reads as one bubble.
    private func upsertUserTurn(text: String, itemId: String?) {
        let t = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !t.isEmpty else { return }
        if let id = itemId, let row = userRows[id],
           let index = turns.firstIndex(where: { $0.id == row }) {
            turns[index].text = t
            if let messageIndex = userMessages[id], messages.indices.contains(messageIndex) {
                messages[messageIndex].text = t
            }
            dirty = true
            scheduleSave()
            lastUserItemId = id
            return
        }
        if let last = turns.last, last.speaker == .you,
           userTurnOpen && Self.isContinuation(of: last.text, next: t) {
            turns[turns.count - 1].text = t
            if let idx = messages.lastIndex(where: { $0.role == "user" }) {
                messages[idx].text = t
            }
            dirty = true
            scheduleSave()
            lastUserItemId = itemId
            bindUserItem(itemId)
            userTurnOpen = true
            return
        }
        if let idx = turns.lastIndex(where: { $0.speaker == .you }),
           turns.count - 1 - idx <= 2,
           Self.isContinuation(of: turns[idx].text, next: t) {
            turns[idx].text = t
            if let mIdx = messages.lastIndex(where: { $0.role == "user" }) {
                messages[mIdx].text = t
            }
            dirty = true
            scheduleSave()
            lastUserItemId = itemId
            bindUserItem(itemId)
            userTurnOpen = true
            return
        }
        lastUserItemId = itemId
        userTurnOpen = true
        append(Turn(speaker: .you, text: t))
        recordUser(t)
        bindUserItem(itemId)
    }

    private func bindUserItem(_ itemId: String?) {
        guard let id = itemId, let row = turns.last(where: { $0.speaker == .you }),
              let index = messages.lastIndex(where: { $0.role == "user" }) else { return }
        userRows[id] = row.id
        userMessages[id] = index
    }

    func displayedText(for turn: Turn) -> String {
        guard let item = interimItemId, userRows[item] == turn.id, let interim else { return turn.text }
        return interim
    }

    var interimHasExistingRow: Bool {
        guard let item = interimItemId, let row = userRows[item] else { return false }
        return turns.contains(where: { $0.id == row })
    }

    /// Whether a new final restates and extends an earlier partial bubble:
    /// same words (STT may revise the spelling), or the old bubble is a
    /// prefix of the restated whole. Adjacency is enforced by the caller.
    private static func isContinuation(of old: String, next newText: String) -> Bool {
        let a = old.lowercased()
        let b = newText.lowercased()
        guard !a.isEmpty, !b.isEmpty else { return false }
        if a == b || (b.count > a.count && b.hasPrefix(a)) { return true }
        return false
    }

    /// Append a tool result to the transcript so it survives into later turns.
    private func recordTool(name: String, summary: String) {
        let trimmed = summary.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        // Keep transcripts lean: a fetch can return 20k characters and the
        // replay only shows the first 500 anyway.
        let capped = trimmed.count > 2000 ? String(trimmed.prefix(1997)) + "..." : trimmed
        userTurnOpen = false
        messages.append(ChatMessage(role: "tool", text: capped, name: name))
        dirty = true
        scheduleSave()
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
            // Detach before flushing. flushSave cancels any pending debounce so
            // a direct save supersedes it — but reached from here, saveTask *is*
            // this task, so it cancelled itself and took the in-flight PATCH
            // down with it. Every debounced save failed with "cancelled"; only
            // the direct calls on session switch or stop ever landed.
            self?.saveTask = nil
            await self?.flushSave()
        }
    }

    func flushSave() async {
        saveTask?.cancel()
        saveTask = nil
        guard dirty, !messages.isEmpty else { return }
        guard let id = await ensureSession() else { return }
        do {
            try await LocalServiceStarter.shared.ensureSidecar()
            try await ChatStore.shared.patchSession(id: id, title: activeSessionTitle, messages: messages)
            dirty = false
        } catch {
            NSLog("[Voice] could not save chat session: \(error.localizedDescription)")
        }
    }

    private func ensureSession() async -> String? {
        if let id = activeSessionId { return id }
        do {
            try await LocalServiceStarter.shared.ensureSidecar()
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
