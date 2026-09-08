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

        let blocked = VoiceToolFormatting.page(["gated": true, "text": "Subscribe", "url": "https://example.com"], source: "fetch")
        let decoded = try JSONSerialization.jsonObject(with: Data(blocked.utf8)) as! [String: Any]
        assert(decoded["status"] as? String == "blocked")
        assert(decoded["complete"] as? Bool == false)
        let partial = VoiceToolFormatting.page(["text": "Article", "truncated": true], source: "fetch")
        let partialResult = try JSONSerialization.jsonObject(with: Data(partial.utf8)) as! [String: Any]
        assert(partialResult["complete"] as? Bool == false)

        var methods = [String]()
        let recovered = try await PageReadWorkflow.read(url: "https://example.com/story", allowFetch: true, allowBridge: true) { method, _ in
            methods.append(method)
            return method == "web_fetch"
                ? ["gated": true, "text": "Subscribe"]
                : ["url": "https://example.com/story#heading", "text": "The requested article"]
        }
        assert(methods == ["web_fetch", "chrome_bridge"])
        assert(recovered["text"] as? String == "The requested article")
        let mismatch = try await PageReadWorkflow.read(url: "https://example.com/story", allowFetch: false, allowBridge: true) { _, _ in
            ["url": "https://example.com/other", "text": "Unrelated page"]
        }
        assert(mismatch["text"] == nil, "Never substitute a different Chrome page")
        methods = []
        _ = try await PageReadWorkflow.read(url: nil, allowFetch: true, allowBridge: true) { method, url in
            methods.append(method)
            if method == "chrome_bridge" { return ["status": "failed", "url": "https://example.com/old"] }
            assert(url == "https://example.com/old")
            return ["text": "Recovered page"]
        }
        assert(methods == ["chrome_bridge", "web_fetch"])
        let missingBridge = try await PageReadWorkflow.read(url: "https://x.com/user/status/123", allowFetch: false, allowBridge: true) { _, _ in
            ["status": "failed", "reason": "bridge_never_enabled"]
        }
        let missingAttempts = missingBridge["attempts"] as! [[String: Any]]
        assert(missingAttempts[0]["reason"] as? String == "bridge_never_enabled")
        assert(missingAttempts[0]["status"] as? String == "failed")
        methods = []
        _ = try await PageReadWorkflow.read(url: nil, allowFetch: false, allowBridge: false) { method, _ in
            methods.append(method); return [:]
        }
        assert(methods.isEmpty, "Disabled tools must not execute")

        assert(!PageReadWorkflow.prefersBrowser("https://x.com.example.org/story"))
        assert(PageReadWorkflow.samePage("https://twitter.com/user/status/123", "https://x.com/user/status/123#body"))
        methods = []
        let xArticle = try await PageReadWorkflow.read(url: "https://twitter.com/user/status/123", allowFetch: true, allowBridge: true) { method, _ in
            methods.append(method)
            return ["url": "https://x.com/user/status/123", "text": "Full X article", "complete": true]
        }
        assert(methods == ["chrome_bridge"] && xArticle["complete"] as? Bool == true)
        methods = []
        _ = try await PageReadWorkflow.read(url: "https://x.com/user/status/123", allowFetch: true, allowBridge: true) { method, address in
            methods.append(method)
            assert(address == "https://x.com/user/status/123")
            return method == "chrome_bridge"
                ? ["url": "https://x.com/other/status/456", "text": "Wrong post"]
                : ["text": "Requested post"]
        }
        assert(methods == ["chrome_bridge", "web_fetch"])
        methods = []
        _ = try await PageReadWorkflow.read(url: "https://x.com/user/status/123", allowFetch: true, allowBridge: false) { method, _ in
            methods.append(method)
            return ["text": "Public post"]
        }
        assert(methods == ["web_fetch"], "Browser preference must respect disabled bridge")
        methods = []
        _ = try await PageReadWorkflow.read(url: "https://example.com/member/story", allowFetch: true, allowBridge: true, preferBrowser: true) { method, url in
            methods.append(method)
            return ["url": url!, "text": "Member article"]
        }
        assert(methods == ["chrome_bridge"])

        methods = []
        let completePage = try await PageReadWorkflow.read(url: "https://example.com/story", allowFetch: true, allowBridge: true) { method, url in
            methods.append(method)
            return method == "web_fetch"
                ? ["text": "Only the beginning", "truncated": true]
                : ["url": url!, "text": "Complete article", "complete": true]
        }
        assert(methods == ["web_fetch", "chrome_bridge"])
        assert(completePage["text"] as? String == "Complete article")
        let retainedPartial = try await PageReadWorkflow.read(url: "https://example.com/story", allowFetch: true, allowBridge: true) { method, _ in
            method == "web_fetch" ? ["text": "Available beginning", "complete": false] : ["status": "failed"]
        }
        assert(retainedPartial["text"] as? String == "Available beginning")
        assert(retainedPartial["complete"] as? Bool == false)
        assert((retainedPartial["attempts"] as? [[String: Any]])?.count == 2)
        let blockedText = try await PageReadWorkflow.read(url: "https://example.com/story", allowFetch: true, allowBridge: false) { _, _ in
            ["status": "blocked", "text": "Please subscribe"]
        }
        assert(blockedText["text"] == nil)

        let cancelledRead = Task {
            try await PageReadWorkflow.read(url: "https://example.com/story", allowFetch: true, allowBridge: true) { _, _ in
                withUnsafeCurrentTask { $0?.cancel() }
                return ["gated": true]
            }
        }
        do {
            _ = try await cancelledRead.value
            assertionFailure("Cancellation must stop page fallback")
        } catch is CancellationError { }

        var screens = ScreenReadBudget()
        assert(screens.accept(signature: "one"))
        assert(!screens.accept(signature: "one"))
        assert(screens.stopped)
        screens = ScreenReadBudget()
        for number in 0..<8 { assert(screens.accept(signature: String(number))) }
        assert(screens.stopped && !screens.accept(signature: "ninth"))

        testTranscript()
        print("Native runtime checks passed: playback, cancellation, page recovery, screen bounds, transcript revisions")
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
