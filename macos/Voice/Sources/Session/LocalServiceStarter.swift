import Foundation

/// Start this checkout's local services before opening the microphone
/// or reading saved chats / personal memory.
/// Custom service addresses remain externally managed.
@MainActor
final class LocalServiceStarter {
    static let shared = LocalServiceStarter()
    /// Full `run-browser.sh` (voice + sidecar).
    private var launcher: Process?
    /// `--sidecar-only` launcher so Settings/history work without loading models.
    private var sidecarLauncher: Process?
    private var log: FileHandle?
    private var starting = false
    private var waiters: [CheckedContinuation<Void, Error>] = []

    /// Keep models warm between conversations, but release our own launcher
    /// when Voice quits. Its trap leaves externally started services alone.
    func stop() {
        starting = false
        let pending = waiters
        waiters = []
        pending.forEach { $0.resume(throwing: CancellationError()) }
        if launcher?.isRunning == true { launcher?.terminate() }
        if sidecarLauncher?.isRunning == true { sidecarLauncher?.terminate() }
        launcher = nil
        sidecarLauncher = nil
        try? log?.close()
        log = nil
    }

    static func manages(voice: URL, sidecar: URL) -> Bool {
        let local = ["localhost", "127.0.0.1"]
        return voice.scheme == "ws" && local.contains(voice.host ?? "") && voice.port == 8766
            && voice.path == "/v1/realtime"
            && managesSidecar(sidecar)
    }

    static func managesSidecar(_ sidecar: URL) -> Bool {
        let local = ["localhost", "127.0.0.1"]
        return sidecar.scheme == "http" && local.contains(sidecar.host ?? "")
            && sidecar.port == 7860 && sidecar.path == "/api"
    }

    /// What a probe of one local service found.
    enum ServiceStatus: Equatable {
        /// Running this checkout's current code and (for voice) models loaded.
        case ready
        /// Current code, still loading.
        case starting
        /// Answering, but not something this app can use: code on disk has
        /// changed since it started, it belongs to another checkout, or it
        /// predates the health contract.
        case stale
        /// Nothing answered.
        case unreachable
    }

    private func fetchJSON(_ url: URL) async -> (code: Int, json: [String: Any]?)? {
        var request = URLRequest(url: url)
        request.timeoutInterval = 0.8
        do {
            let (data, response) = try await URLSession.shared.data(for: request)
            let code = (response as? HTTPURLResponse)?.statusCode ?? 0
            return (code, try? JSONSerialization.jsonObject(with: data) as? [String: Any])
        } catch { return nil }
    }

    /// Whether a health payload describes this checkout's current code.
    /// Older servers report no fingerprint, so they cannot be current.
    static func runsCurrentCode(_ json: [String: Any], root: String?) -> Bool {
        guard let fingerprint = json["fingerprint"] as? String, !fingerprint.isEmpty,
              json["stale"] as? Bool == false,
              let source = json["source"] as? String, let root else { return false }
        return samePath(source, root)
    }

    static func samePath(_ a: String, _ b: String) -> Bool {
        URL(fileURLWithPath: a).resolvingSymlinksInPath().standardizedFileURL.path
            == URL(fileURLWithPath: b).resolvingSymlinksInPath().standardizedFileURL.path
    }

    static func sidecarStatus(code: Int, json: [String: Any]?, root: String?) -> ServiceStatus {
        guard code == 200, let json, json["chatbotUrl"] != nil else { return .stale }
        return runsCurrentCode(json, root: root) ? .ready : .stale
    }

    static func voiceStatus(code: Int, json: [String: Any]?, root: String?) -> ServiceStatus {
        guard code == 200, let json, json["ready"] != nil else { return .stale }
        guard runsCurrentCode(json, root: root) else { return .stale }
        let ready = json["ready"] as? Bool == true
        return ready ? .ready : .starting
    }

    private func sidecarStatus(_ sidecar: URL) async -> ServiceStatus {
        guard let reply = await fetchJSON(sidecar.appendingPathComponent("config")) else { return .unreachable }
        return Self.sidecarStatus(code: reply.code, json: reply.json, root: try? repositoryRoot())
    }

    private func voiceStatus(_ voice: URL) async -> ServiceStatus {
        var address = URLComponents(url: voice, resolvingAgainstBaseURL: false)!
        address.scheme = "http"
        address.query = nil
        address.path = "/health"
        guard let healthURL = address.url, let reply = await fetchJSON(healthURL) else { return .unreachable }
        return Self.voiceStatus(code: reply.code, json: reply.json, root: try? repositoryRoot())
    }

    private struct Readiness {
        var voice: ServiceStatus
        var sidecar: ServiceStatus

        /// A conversation needs both services on current code. Settings,
        /// history and memory only need a sidecar that answers: replacing a
        /// stale one there could take a live conversation's backend down with
        /// it, so stale services are replaced at conversation start instead.
        func satisfied(needVoice: Bool) -> Bool {
            if needVoice { return sidecar == .ready && voice == .ready }
            return sidecar != .unreachable
        }

        /// A running service that must be replaced before waiting on it helps.
        func needsRestart(needVoice: Bool) -> Bool {
            needVoice && (sidecar == .stale || voice == .stale)
        }
    }

    private func probe(needVoice: Bool) async -> Readiness {
        let sidecar = LocalService.sidecarAPI
        let voice = LocalService.voiceWebSocket
        async let sidecarState = sidecarStatus(sidecar)
        if needVoice {
            async let voiceState = voiceStatus(voice)
            return await Readiness(voice: voiceState, sidecar: sidecarState)
        }
        return Readiness(voice: .unreachable, sidecar: await sidecarState)
    }

    /// Bring up the FastAPI sidecar so saved chats and personal memory load
    /// without waiting on the speech models. No-op for a custom sidecar URL.
    func ensureSidecar() async throws {
        let sidecar = LocalService.sidecarAPI
        guard Self.managesSidecar(sidecar) else { return }
        if await probe(needVoice: false).satisfied(needVoice: false) { return }
        try await bringUp(needVoice: false)
    }

    func ensureReady(voice: URL, sidecar: URL) async throws {
        guard Self.manages(voice: voice, sidecar: sidecar) else { return }
        if await probe(needVoice: true).satisfied(needVoice: true) { return }
        try await bringUp(needVoice: true)
    }

    private func bringUp(needVoice: Bool) async throws {
        try Task.checkCancellation()
        var state = await probe(needVoice: needVoice)
        if state.satisfied(needVoice: needVoice) { return }
        if starting {
            try await withCheckedThrowingContinuation { waiters.append($0) }
            state = await probe(needVoice: needVoice)
            if state.satisfied(needVoice: needVoice) { return }
        }
        starting = true
        defer {
            starting = false
            let pending = waiters
            waiters = []
            pending.forEach { $0.resume() }
        }
        if state.needsRestart(needVoice: needVoice) {
            // The launcher replaces stale services it finds, but a launcher
            // of ours would otherwise keep supervising the ones it replaces.
            await stopOwnLaunchers()
        }
        try spawnLauncher(needVoice: needVoice)
        try await waitUntilReady(needVoice: needVoice)
    }

    /// Terminate launchers this app started and wait for them to exit; their
    /// EXIT traps stop the services they own.
    private func stopOwnLaunchers() async {
        let running = [launcher, sidecarLauncher].compactMap { $0 }.filter(\.isRunning)
        running.forEach { $0.terminate() }
        let deadline = Date().addingTimeInterval(15)
        while Date() < deadline, running.contains(where: \.isRunning) {
            try? await Task.sleep(nanoseconds: 100_000_000)
        }
        launcher = nil
        sidecarLauncher = nil
    }

    private func spawnLauncher(needVoice: Bool) throws {
        if needVoice {
            if launcher?.isRunning == true { return }
        } else if sidecarLauncher?.isRunning == true || launcher?.isRunning == true {
            return
        }
        let path = try repositoryRoot()
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/bash")
        var arguments = [path + "/run-browser.sh", "--reuse-running"]
        if !needVoice { arguments.append("--sidecar-only") }
        process.arguments = arguments
        process.currentDirectoryURL = URL(fileURLWithPath: path)
        var environment = ProcessInfo.processInfo.environment
        // Match the child's working directory so Bash need not reconstruct
        // it by walking protected parent directories on macOS.
        environment["PWD"] = path
        environment["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        environment["PORT"] = "8766"
        environment["WEB_PORT"] = "7860"
        process.environment = environment
        let logURL = URL(fileURLWithPath: "/tmp/voice-service-startup.log")
        FileManager.default.createFile(atPath: logURL.path, contents: nil)
        try? log?.close()
        log = try FileHandle(forWritingTo: logURL)
        process.standardOutput = log
        process.standardError = log
        try process.run()
        if needVoice {
            launcher = process
        } else {
            sidecarLauncher = process
        }
    }

    private func waitUntilReady(needVoice: Bool) async throws {
        let deadline = Date().addingTimeInterval(needVoice ? 180 : 45)
        let child = needVoice ? launcher : (launcher ?? sidecarLauncher)
        while Date() < deadline {
            try Task.checkCancellation()
            if await probe(needVoice: needVoice).satisfied(needVoice: needVoice) { return }
            if let child, !child.isRunning {
                if await probe(needVoice: needVoice).satisfied(needVoice: needVoice) { return }
                throw StartupError("Local service startup failed. Check /tmp/voice-service-startup.log and /tmp/chatbot-server.log.")
            }
            try await Task.sleep(nanoseconds: 200_000_000)
        }
        throw StartupError(needVoice
            ? "The local services are still starting. Check /tmp/chatbot-server.log, then try again."
            : "The local sidecar is still starting. Check /tmp/chatbot-web.log, then try again.")
    }

    private func repositoryRoot() throws -> String {
        guard let location = Bundle.main.url(forResource: "RepositoryPath", withExtension: "txt"),
              let path = try? String(contentsOf: location, encoding: .utf8).trimmingCharacters(in: .whitespacesAndNewlines),
              FileManager.default.isExecutableFile(atPath: path + "/run-browser.sh") else {
            throw StartupError("Local services are stopped. Rebuild Voice from your project, or start run-browser.sh.")
        }
        return path
    }

    struct StartupError: LocalizedError {
        let message: String
        init(_ message: String) { self.message = message }
        var errorDescription: String? { message }
    }
}
