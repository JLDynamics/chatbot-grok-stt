import Foundation

@main
struct RuntimeTests {
    @MainActor
    static func main() async throws {
        let standardVoice = URL(string: "ws://127.0.0.1:8766/v1/realtime")!
        let standardTools = URL(string: "http://127.0.0.1:7860/api")!
        assert(LocalServiceStarter.manages(voice: standardVoice, sidecar: standardTools))
        assert(!LocalServiceStarter.manages(voice: URL(string: "wss://example.com/v1/realtime")!, sidecar: standardTools))
        assert(!LocalServiceStarter.manages(voice: standardVoice, sidecar: URL(string: "http://127.0.0.1:7960/api")!))
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
        for number in 0..<6 { assert(screens.accept(signature: String(number))) }
        assert(screens.stopped && !screens.accept(signature: "seventh"))

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
