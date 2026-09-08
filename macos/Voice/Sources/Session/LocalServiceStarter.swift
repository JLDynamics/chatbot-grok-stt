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

    private func reachable(_ url: URL, expectedKey: String) async -> Bool {
        var request = URLRequest(url: url)
        request.timeoutInterval = 2
        do {
            let (data, response) = try await URLSession.shared.data(for: request)
            guard (response as? HTTPURLResponse)?.statusCode == 200,
                  let json = try JSONSerialization.jsonObject(with: data) as? [String: Any] else { return false }
            return json[expectedKey] != nil
        } catch { return false }
    }

    private func sidecarReady(_ sidecar: URL) async -> Bool {
        await reachable(sidecar.appendingPathComponent("config"), expectedKey: "chatbotUrl")
    }

    private func ready(voice: URL, sidecar: URL) async -> Bool {
        var address = URLComponents(url: voice, resolvingAgainstBaseURL: false)!
        address.scheme = "http"
        address.path = "/openapi.json"
        address.query = nil
        let healthURL = address.url!
        async let voiceReady = reachable(healthURL, expectedKey: "openapi")
        async let toolsReady = sidecarReady(sidecar)
        let readiness = await (voiceReady, toolsReady)
        return readiness.0 && readiness.1
    }

    /// Bring up the FastAPI sidecar so saved chats and personal memory load
    /// without waiting on Parakeet/TTS. No-op for a custom sidecar URL.
    func ensureSidecar() async throws {
        let sidecar = LocalService.sidecarAPI
        guard Self.managesSidecar(sidecar) else { return }
        if await sidecarReady(sidecar) { return }
        try await bringUp(needVoice: false)
    }

    func ensureReady(voice: URL, sidecar: URL) async throws {
        guard Self.manages(voice: voice, sidecar: sidecar) else { return }
        if await ready(voice: voice, sidecar: sidecar) { return }
        try await bringUp(needVoice: true)
    }

    private func bringUp(needVoice: Bool) async throws {
        try Task.checkCancellation()
        let sidecar = LocalService.sidecarAPI
        let voice = LocalService.voiceWebSocket
        if needVoice {
            if await ready(voice: voice, sidecar: sidecar) { return }
        } else if await sidecarReady(sidecar) {
            return
        }
        if starting {
            try await withCheckedThrowingContinuation { waiters.append($0) }
            if needVoice {
                if await ready(voice: voice, sidecar: sidecar) { return }
            } else if await sidecarReady(sidecar) {
                return
            }
        }
        starting = true
        defer {
            starting = false
            let pending = waiters
            waiters = []
            pending.forEach { $0.resume() }
        }
        try spawnLauncher(needVoice: needVoice)
        try await waitUntilReady(needVoice: needVoice)
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
        let sidecar = LocalService.sidecarAPI
        let voice = LocalService.voiceWebSocket
        let attempts = needVoice ? 180 : 45
        let child = needVoice ? launcher : (launcher ?? sidecarLauncher)
        for _ in 0..<attempts {
            try Task.checkCancellation()
            if needVoice {
                if await ready(voice: voice, sidecar: sidecar) { return }
            } else if await sidecarReady(sidecar) {
                return
            }
            if let child, !child.isRunning {
                if needVoice {
                    if await ready(voice: voice, sidecar: sidecar) { return }
                } else if await sidecarReady(sidecar) {
                    return
                }
                throw StartupError("Local service startup failed. Check /tmp/voice-service-startup.log and /tmp/chatbot-server.log.")
            }
            try await Task.sleep(nanoseconds: 1_000_000_000)
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
