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
            from: Data(#"{"search":true,"desktopControl":true,"chatbotUrl":"ws://x"}"#.utf8)
        )
        assert(config.search && config.desktopControl)
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
        // client only publishes definitions and runs screenshot.
        let names = VoiceToolExecutor.shared.activeToolDefinitions().compactMap { $0["name"] as? String }
        assert(Set(names).count == names.count, "Tool names must be unique")
        assert(names.contains("remember") && names.contains("forget") && names.contains("search_chat_history"))
        assert(names.contains("bash"), "This branch publishes bash for public research")
        assert(!names.contains("web_search"))
        assert(names.contains("read_page"), "The Chrome page bridge is exposed as read_page")
        let bash = VoiceToolExecutor.shared.activeToolDefinitions().first { $0["name"] as? String == "bash" }
        let bashDesc = bash?["description"] as? String ?? ""
        assert(bashDesc.contains("when:1d"), "bash tool must tell the model to date-filter news")
        assert(bashDesc.contains("Wikipedia"), "office-holder facts should fetch Wikipedia, not a news feed")
        assert(bashDesc.contains("voice model") || bashDesc.contains("Research the voice model"),
               "bash is the voice model's research tool")
        assert(bashDesc.contains("read_page"), "bash description must point article reading at read_page")
        let readPage = VoiceToolExecutor.shared.activeToolDefinitions().first { $0["name"] as? String == "read_page" }
        let readPageDesc = readPage?["description"] as? String ?? ""
        assert(readPageDesc.contains("Chrome"), "read_page is the live Chrome tab")
        assert(!names.contains("code_agent"), "The coding agent was removed; Claude Code covers that job")
        assert(!names.contains("web_fetch") && !names.contains("read_article"), "Legacy page tools are gone")
        for name in names where VoiceToolExecutor.serverSideTools.contains(name) {
            assert(["bash", "read_page", "remember", "forget", "search_chat_history"].contains(name))
        }
        let unavailable = await VoiceToolExecutor.shared.run(name: "web_search", argsJson: "{\"query\":\"x\"}")
        assert(unavailable.output.contains("runs on the server"), "Research tools never execute in the app")

        testNoiseGate()
        testMicCapture()
        testSpeechStartPolicy()
        testTranscript()
        print("Native runtime checks passed: playback, cancellation, tool definitions, transcript revisions")
    }

    static func pcm16(_ samples: [Int16]) -> [UInt8] {
        var bytes = [UInt8]()
        bytes.reserveCapacity(samples.count * 2)
        for sample in samples {
            let raw = UInt16(bitPattern: sample)
            bytes.append(UInt8(raw & 0xff))
            bytes.append(UInt8((raw >> 8) & 0xff))
        }
        return bytes
    }

    static func sinePCM(hz: Double, amplitude: Int16, count: Int, sampleRate: Double = 16_000) -> [UInt8] {
        let step = 2 * Double.pi * hz / sampleRate
        let samples: [Int16] = (0..<count).map { i in
            Int16((sin(Double(i) * step) * Double(amplitude)).rounded())
        }
        return pcm16(samples)
    }

    static func testNoiseGate() {
        let gate = PCM16NoiseGate(thresholdDBFS: -48, isEnabled: true)
        gate.reset()
        let quiet = sinePCM(hz: 80, amplitude: 20, count: 512)
        let (quietBytes, quietOpen) = gate.process(quiet)
        assert(!quietOpen, "Room-level noise must not open the mic gate")
        assert(quietBytes.allSatisfy { $0 == 0 }, "Closed gate must zero the samples")

        // Loud but muffled (through-floor). Energy-only gates open on this.
        let distant = sinePCM(hz: 80, amplitude: 8_000, count: 512)
        for step in 1...5 {
            let (_, open) = gate.process(distant)
            assert(!open, "Muffled far-field speech must stay closed after \(step) chunks")
        }

        // DC has energy and zero brightness — not close speech.
        let dc = pcm16([Int16](repeating: 4_000, count: 512))
        let (_, dcOpen) = gate.process(dc)
        assert(!dcOpen, "A loud DC offset is not close-talk")

        // Nearby speech is loud and bright enough to open immediately.
        let speech = sinePCM(hz: 500, amplitude: 4_000, count: 512)
        let (openBytes, speechOpen) = gate.process(speech)
        assert(speechOpen, "Close speech must open the mic gate")
        assert(openBytes == speech, "An open gate must not copy-transform the samples")

        let (heldBytes, heldOpen) = gate.process(quiet)
        assert(heldOpen, "Hold must keep the gate open after speech")
        assert(heldBytes == quiet)

        var stillOpen = true
        for _ in 0..<12 {
            stillOpen = gate.process(quiet).isOpen
        }
        assert(!stillOpen, "Hold must expire after the talker stops")

        let disabled = PCM16NoiseGate(thresholdDBFS: -48, isEnabled: false)
        let (passthrough, open) = disabled.process(quiet)
        assert(open && passthrough == quiet, "A disabled gate is a no-op")
    }

    static func testMicCapture() {
        let capture = MicCapture()
        let gen = UUID()
        capture.arm(generation: gen)
        capture.setAccepting(true)
        let speech = sinePCM(hz: 500, amplitude: 4_000, count: 640)
        var sent: [UInt8]?
        for _ in 0..<4 {
            if let chunk = capture.ingest(speech, generation: gen) { sent = chunk }
        }
        assert(sent != nil, "Accepting capture must emit a batched chunk")
        capture.setMuted(true)
        assert(capture.ingest(speech, generation: gen) == nil, "Mute must drop the next ingest without waiting")
        capture.disarm()
        assert(capture.ingest(speech, generation: gen) == nil, "Disarmed capture must drop audio")
    }

    static func testSpeechStartPolicy() {
        assert(
            !VoiceSpeechStartPolicy.shouldShowListening(responseActive: true, playing: false),
            "TTS chunk gaps during a response must not look like Listening"
        )
        assert(
            !VoiceSpeechStartPolicy.shouldShowListening(responseActive: false, playing: true)
        )
        assert(
            VoiceSpeechStartPolicy.shouldShowListening(responseActive: false, playing: false)
        )
        assert(!VoiceSpeechStartPolicy.shouldInterruptLocalPlayback(responseActive: true))
        assert(VoiceSpeechStartPolicy.shouldInterruptLocalPlayback(responseActive: false))
    }

    @MainActor
    static func testTranscript() {
        let backend = MockVoiceBackend()
        let session = SessionController(backend: backend, restoreSavedSession: false)
        backend.onUserSpeechStarted?()
        backend.onUserFinal?("I want to explain", "turn-one")
        // A final is held until the turn settles. Pausing mid-sentence finalizes
        // each revision, and painting every one rewrote the bubble while the
        // user was still talking.
        assert(session.turns.isEmpty, "A finalized turn waits for the turn to settle")
        assert(session.userSpeaking, "The indicator stays up while the turn is held")
        backend.onAgentDelta?("Go ahead, I am listening carefully to the microphone problem.")
        assert(!session.userSpeaking, "Painting the words lowers the indicator")
        let original = session.turns[0].id
        assert(session.turns[0].text == "I want to explain")
        backend.onAgentDone?()
        backend.onUserFinal?("I want to explain the microphone problem", "turn-one")
        backend.onTurnDropped?()
        assert(session.turns.count == 2 && session.turns[0].id == original)
        backend.onUserFinal?("Now read this other page", "turn-two")
        backend.onTurnDropped?()
        assert(session.turns.count == 3, "A distinct utterance must not overwrite earlier speech")

        // Pausing repeatedly inside one turn must not append the turn to itself.
        // Every revision's final is decoded from all of that turn's audio, and
        // STT revises words between passes ("thing" -> "things over there").
        // That defeated the merge heuristic, which then appended one more copy
        // of the whole turn on each pause until the bubble was unreadable.
        let repeatBackend = MockVoiceBackend()
        let repeats = SessionController(backend: repeatBackend, restoreSavedSession: false)
        let head = "i'm sorry i have to interrupt you i need to test this feature"
        repeatBackend.onUserSpeechStarted?()
        repeatBackend.onUserFinal?(head, "turn-pause")
        repeatBackend.onUserFinal?("\(head) and see whether it's good it's still the other thing", "turn-pause")
        repeatBackend.onUserFinal?("\(head) and see whether it's good it's still are the other things over there", "turn-pause")
        assert(repeats.turns.isEmpty, "Revisions must not paint while the turn is still open")
        assert(repeats.userSpeaking, "The indicator covers the pauses")
        repeatBackend.onTurnDropped?()
        let shown = repeats.turns[0].text
        assert(repeats.turns.count == 1, "Pausing inside one turn must not open more bubbles")
        assert(
            shown == "\(head) and see whether it's good it's still are the other things over there",
            "Each revision must replace the bubble; got: \(shown)"
        )
        assert(
            shown.components(separatedBy: "i'm sorry").count - 1 == 1,
            "The turn must appear exactly once, not once per pause"
        )

        let pauseBackend = MockVoiceBackend()
        let pauseSession = SessionController(backend: pauseBackend, restoreSavedSession: false)
        pauseBackend.onUserFinal?("yeah i still need to finish", "turn-a")
        pauseBackend.onUserFinal?("a lot of work to do", "turn-b")
        pauseBackend.onTurnDropped?()
        assert(pauseSession.turns.count == 1, "A paused continuation must stay one bubble")
        assert(pauseSession.turns[0].text.contains("yeah i still need to finish"))
        assert(pauseSession.turns[0].text.contains("a lot of work to do"))

        let fillerBackend = MockVoiceBackend()
        let fillerSession = SessionController(backend: fillerBackend, restoreSavedSession: false)
        fillerBackend.onUserFinal?("i still need to finish", "turn-fill-a")
        fillerBackend.onAgentDelta?("...")
        fillerBackend.onAgentDone?()
        fillerBackend.onUserFinal?("a lot of work today", "turn-fill-b")
        fillerBackend.onTurnDropped?()
        assert(fillerSession.turns.count == 2, "Keep the tiny agent filler row")
        assert(fillerSession.turns[0].speaker == .you)
        assert(fillerSession.turns[0].text.contains("i still need to finish"))
        assert(fillerSession.turns[0].text.contains("a lot of work today"))
        assert(fillerSession.turns[1].text == "...")

        let restateBackend = MockVoiceBackend()
        let restateSession = SessionController(backend: restateBackend, restoreSavedSession: false)
        restateBackend.onUserFinal?("i still need to finish a lot of work to do", "turn-restate")
        restateBackend.onUserFinal?("i still need to finish a lot of work today", "turn-restate")
        restateBackend.onTurnDropped?()
        assert(restateSession.turns.count == 1)
        assert(restateSession.turns[0].text == "i still need to finish a lot of work today")

        let splitBackend = MockVoiceBackend()
        let splitSession = SessionController(backend: splitBackend, restoreSavedSession: false)
        splitBackend.onUserFinal?("yeah i still need to finish", "turn-split-a")
        splitBackend.onTurnDropped?()
        splitBackend.onUserFinal?("a lot of work to do", "turn-split-b")
        splitBackend.onTurnDropped?()
        assert(splitSession.turns.count == 1, "A paused continuation must stay one bubble")
        assert(splitSession.turns[0].text.contains("yeah i still need to finish"))
        assert(splitSession.turns[0].text.contains("a lot of work to do"))

        // The talking indicator is the only speaking feedback, so it must never
        // strand: every way a turn can end has to lower it.
        let barsBackend = MockVoiceBackend()
        let bars = SessionController(backend: barsBackend, restoreSavedSession: false)
        assert(!bars.userSpeaking, "Idle shows no indicator")
        barsBackend.onUserSpeechStarted?()
        assert(bars.userSpeaking, "Speech start raises the indicator")
        barsBackend.onTurnDropped?()
        assert(!bars.userSpeaking, "A turn the server drops lowers the indicator")
        barsBackend.onUserSpeechStarted?()
        barsBackend.onUserFinal?("what is left on my list", "turn-bars")
        assert(bars.userSpeaking, "A held final keeps the indicator up through a pause")
        assert(bars.turns.isEmpty, "and paints nothing yet")
        barsBackend.onAgentDelta?("Two things.")
        assert(!bars.userSpeaking, "Painting the words lowers the indicator")
        assert(bars.turns.first?.text == "what is left on my list")
        barsBackend.onUserSpeechStarted?()
        barsBackend.onUserFinal?("one more thing", "turn-bars-2")
        assert(bars.userSpeaking)
        bars.requestEnd()
        assert(!bars.userSpeaking, "Stopping lowers the indicator")
        assert(
            bars.turns.contains { $0.text.contains("one more thing") },
            "Stopping must paint held words rather than losing them"
        )

        let junkBackend = MockVoiceBackend()
        let junkSession = SessionController(backend: junkBackend, restoreSavedSession: false)
        // The server drops filler before it reaches the client, so a dropped
        // turn must leave no bubble and no indicator behind.
        junkBackend.onUserSpeechStarted?()
        junkBackend.onTurnDropped?()
        assert(junkSession.turns.isEmpty, "A dropped turn leaves no bubble")
        assert(!junkSession.userSpeaking, "A dropped turn lowers the indicator")
    }
}
