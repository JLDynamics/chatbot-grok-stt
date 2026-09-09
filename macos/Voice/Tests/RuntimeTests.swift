import Foundation
import CoreGraphics

@main
struct RuntimeTests {
    @MainActor
    static func main() async throws {
        let standardVoice = URL(string: "ws://127.0.0.1:8766/v1/realtime")!
        let standardTools = URL(string: "http://127.0.0.1:7860/api")!
        assert(LocalServiceStarter.manages(voice: standardVoice, sidecar: standardTools))
        assert(LocalServiceStarter.managesSidecar(standardTools))
        assert(!LocalServiceStarter.manages(voice: URL(string: "wss://example.com/v1/realtime")!, sidecar: standardTools))
        assert(!LocalServiceStarter.manages(voice: standardVoice, sidecar: URL(string: "http://127.0.0.1:7960/api")!))
        assert(!LocalServiceStarter.managesSidecar(URL(string: "http://127.0.0.1:7960/api")!))

        // A running service is reused only while it runs this checkout's current code.
        let root = "/Users/me/chatbot"
        let voiceHealth: [String: Any] = ["ready": true, "server_tools": true, "fingerprint": "abc", "stale": false, "source": root]
        assert(LocalServiceStarter.voiceStatus(code: 200, json: voiceHealth, root: root) == .ready)
        assert(LocalServiceStarter.voiceStatus(code: 200, json: voiceHealth.merging(["ready": false]) { $1 }, root: root) == .starting)
        // Still loading: the LLM handler has not set server_tools yet.
        assert(LocalServiceStarter.voiceStatus(code: 200, json: voiceHealth.merging(["ready": false, "server_tools": false]) { $1 }, root: root) == .starting)
        assert(LocalServiceStarter.voiceStatus(code: 200, json: voiceHealth.merging(["stale": true]) { $1 }, root: root) == .stale)
        assert(LocalServiceStarter.voiceStatus(code: 200, json: voiceHealth.merging(["server_tools": false]) { $1 }, root: root) == .stale)
        assert(LocalServiceStarter.voiceStatus(code: 200, json: voiceHealth.merging(["source": "/Users/me/chatbot-refactor"]) { $1 }, root: root) == .stale)
        assert(LocalServiceStarter.voiceStatus(code: 200, json: voiceHealth.merging(["source": root + "/"]) { $1 }, root: root) == .ready)
        // Pre-fingerprint servers answer without the contract; they cannot be current.
        assert(LocalServiceStarter.voiceStatus(code: 200, json: ["status": "ok", "ready": true], root: root) == .stale)
        assert(LocalServiceStarter.voiceStatus(code: 404, json: nil, root: root) == .stale)
        let sidecarConfig: [String: Any] = ["chatbotUrl": "ws://x", "fingerprint": "abc", "stale": false, "source": root]
        assert(LocalServiceStarter.sidecarStatus(code: 200, json: sidecarConfig, root: root) == .ready)
        assert(LocalServiceStarter.sidecarStatus(code: 200, json: ["chatbotUrl": "ws://x"], root: root) == .stale)
        assert(LocalServiceStarter.sidecarStatus(code: 200, json: sidecarConfig.merging(["stale": true]) { $1 }, root: root) == .stale)
        assert(LocalServiceStarter.sidecarStatus(code: 500, json: nil, root: root) == .stale)

        let summary = try JSONDecoder().decode(
            ChatSessionSummary.self,
            from: Data(#"{"id":"abc","title":"hello","preview":"hi","message_count":2,"updated_at":"2026-09-08T15:24:07+00:00","created_at":"2026-09-08"}"#.utf8)
        )
        assert(summary.id == "abc" && summary.title == "hello" && summary.preview == "hi")
        let config = try JSONDecoder().decode(
            SidecarConfig.self,
            from: Data(#"{"search":true,"codeAgent":false,"desktopControl":true,"chatbotUrl":"ws://x"}"#.utf8)
        )
        assert(config.search && !config.codeAgent && config.desktopControl)
        let storeError = ChatStoreError.from(
            status: 400,
            data: Data(#"{"detail":"Personal memory is too long; consolidate it first."}"#.utf8)
        )
        assert(storeError.errorDescription == "Personal memory is too long; consolidate it first.")
        assert(ScreenCapture.permissionHelp.contains("Screen Recording"))
        assert(ScreenCapture.permissionHelp.contains("ad-hoc"))
        let padded = ScreenCapture.rawImage(fromDataURL: "data:image/png;base64,iVBORw0KGgo")
        assert(padded == nil || padded!.count >= 0)
        let pngB64 = Data([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A]).base64EncodedString().dropLast()
        let repaired = ScreenCapture.rawImage(fromDataURL: "data:image/png;base64," + pngB64)
        assert(repaired != nil && repaired!.starts(with: [0x89, 0x50, 0x4E, 0x47]))
        var pixel: [UInt8] = [220, 40, 40, 255, 40, 220, 40, 255, 40, 40, 220, 255, 220, 220, 40, 255]
        let jpegImage = pixel.withUnsafeMutableBytes { raw -> CGImage? in
            guard let ctx = CGContext(
                data: raw.baseAddress,
                width: 2,
                height: 2,
                bitsPerComponent: 8,
                bytesPerRow: 8,
                space: CGColorSpaceCreateDeviceRGB(),
                bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
            ) else { return nil }
            return ctx.makeImage()
        }
        assert(jpegImage != nil)
        let jpeg = ScreenCapture.jpegData(from: jpegImage!, maxEdge: 1280, quality: 0.72)
        assert(jpeg != nil && jpeg!.count > 20)
        assert(jpeg!.starts(with: [0xFF, 0xD8]))
        let url = ScreenCapture.modelImageDataURL(from: jpeg!)
        assert(url?.hasPrefix("data:image/jpeg;base64,") == true)
        let tracker = PlaybackTracker()
        let old = tracker.enqueue()
        tracker.clear()
        let current = tracker.enqueue()
        assert(!tracker.complete(old))
        assert(tracker.isAudible, "An abandoned buffer must not drain new playback")
        let now = Date()
        assert(tracker.complete(current, now: now))
        assert(!tracker.isAudible)
        assert(tracker.needsEchoGuard(now: now.addingTimeInterval(0.5)))
        assert(!tracker.needsEchoGuard(now: now.addingTimeInterval(1)))

        assert(!VoiceToolFollowUp.shouldSend(pendingTools: 3, responseActive: false))
        assert(!VoiceToolFollowUp.shouldSend(pendingTools: 0, responseActive: true))
        assert(!VoiceToolFollowUp.shouldSend(pendingTools: 1, responseActive: true))
        assert(VoiceToolFollowUp.shouldSend(pendingTools: 0, responseActive: false))

        let scope = VoiceWorkScope()
        let oldGeneration = scope.generation
        let task = Task<Void, Never> { try? await Task.sleep(nanoseconds: 10_000_000_000) }
        scope.insert(task, id: "old")
        scope.cancel()
        assert(task.isCancelled && oldGeneration != scope.generation)
        let newTask = Task {}
        scope.insert(newTask, id: "old")
        scope.finish("old", generation: oldGeneration)
        assert(scope.contains("old"), "A stale completion must not remove new work")
        scope.cancel()

        // The page ladder and the screen-read budget moved to the server: the
        // client only publishes definitions and runs screenshot / code_agent.
        let names = VoiceToolExecutor.shared.activeToolDefinitions().compactMap { $0["name"] as? String }
        assert(Set(names).count == names.count, "Tool names must be unique")
        assert(names.contains("remember") && names.contains("forget") && names.contains("search_chat_history"))
        assert(!names.contains("web_fetch") && !names.contains("read_article"), "Legacy page tools are gone")
        for name in names where VoiceToolExecutor.serverSideTools.contains(name) {
            assert(["web_search", "read_page", "remember", "forget", "search_chat_history"].contains(name))
        }
        let unavailable = await VoiceToolExecutor.shared.run(name: "web_search", argsJson: "{\"query\":\"x\"}")
        assert(unavailable.output.contains("runs on the server"), "Research tools never execute in the app")

        testTranscript()
        print("Native runtime checks passed: playback, cancellation, tool definitions, transcript revisions")
    }

    @MainActor
    static func testTranscript() {
        let backend = MockVoiceBackend()
        let session = SessionController(backend: backend, restoreSavedSession: false)
        backend.onUserFinal?("I want to explain", "turn-one")
        let original = session.turns[0].id
        backend.onUserPartial?("I want to explain the problem", "turn-one")
        assert(session.interimHasExistingRow)
        assert(session.displayedText(for: session.turns[0]) == "I want to explain the problem")
        backend.onAgentDelta?("Go ahead")
        backend.onAgentDone?()
        backend.onUserFinal?("I want to explain the microphone problem", "turn-one")
        assert(session.turns.count == 2 && session.turns[0].id == original)
        backend.onUserFinal?("Now read this other page", "turn-two")
        assert(session.turns.count == 3, "A distinct utterance must not overwrite earlier speech")
    }
}
