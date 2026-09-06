import Foundation

/// Start this checkout's local services before opening the microphone.
/// Custom service addresses remain externally managed.
@MainActor
final class LocalServiceStarter {
    static let shared = LocalServiceStarter()
    private var launcher: Process?
    private var log: FileHandle?

    /// Keep models warm between conversations, but release our own launcher
    /// when Voice quits. Its trap leaves externally started services alone.
    func stop() {
        if launcher?.isRunning == true { launcher?.terminate() }
        launcher = nil
        try? log?.close()
        log = nil
    }

    static func manages(voice: URL, sidecar: URL) -> Bool {
        let local = ["localhost", "127.0.0.1"]
        return voice.scheme == "ws" && local.contains(voice.host ?? "") && voice.port == 8766
            && voice.path == "/v1/realtime"
            && sidecar.scheme == "http" && local.contains(sidecar.host ?? "")
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

    private func ready(voice: URL, sidecar: URL) async -> Bool {
        var address = URLComponents(url: voice, resolvingAgainstBaseURL: false)!
        address.scheme = "http"
        address.path = "/openapi.json"
        address.query = nil
        let healthURL = address.url!
        async let voiceReady = reachable(healthURL, expectedKey: "openapi")
        async let toolsReady = reachable(sidecar.appendingPathComponent("config"), expectedKey: "chatbotUrl")
        let readiness = await (voiceReady, toolsReady)
        return readiness.0 && readiness.1
    }

    func ensureReady(voice: URL, sidecar: URL) async throws {
        guard Self.manages(voice: voice, sidecar: sidecar) else { return }
        if await ready(voice: voice, sidecar: sidecar) { return }
        try Task.checkCancellation()
        if launcher?.isRunning != true {
            guard let location = Bundle.main.url(forResource: "RepositoryPath", withExtension: "txt"),
                  let path = try? String(contentsOf: location, encoding: .utf8).trimmingCharacters(in: .whitespacesAndNewlines),
                  FileManager.default.isExecutableFile(atPath: path + "/run-browser.sh") else {
                throw StartupError("Local services are stopped. Rebuild Voice from your project, or start run-browser.sh.")
            }
            let process = Process()
            process.executableURL = URL(fileURLWithPath: "/bin/bash")
            process.arguments = [path + "/run-browser.sh", "--reuse-running"]
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
            launcher = process
        }
        for _ in 0..<180 {
            try Task.checkCancellation()
            if await ready(voice: voice, sidecar: sidecar) { return }
            if let launcher, !launcher.isRunning {
                throw StartupError("Local service startup failed. Check /tmp/voice-service-startup.log and /tmp/chatbot-server.log.")
            }
            try await Task.sleep(nanoseconds: 1_000_000_000)
        }
        throw StartupError("The local services are still starting. Check /tmp/chatbot-server.log, then try again.")
    }

    struct StartupError: LocalizedError {
        let message: String
        init(_ message: String) { self.message = message }
        var errorDescription: String? { message }
    }
}
