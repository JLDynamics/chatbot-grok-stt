import AppKit
import Foundation

/// NDJSON pipe between headless Voice and the Pi `/voice` loop.
@MainActor
final class HeadlessBridge {
    static let shared = HeadlessBridge()

    private var session: SessionController?
    private var stdin: DispatchSourceRead?

    func attach(session: SessionController) {
        self.session = session
        let priorFinal = session.backend.onUserFinal
        session.backend.onUserFinal = { text, itemId in
            priorFinal?(text, itemId)
            HeadlessBridge.emit(["type": "transcript", "text": text, "item_id": itemId ?? ""])
        }
        let priorSpeech = session.backend.onUserSpeechStarted
        session.backend.onUserSpeechStarted = {
            priorSpeech?()
            HeadlessBridge.emit(["type": "speech_started"])
        }
        listenStdin()
        HeadlessBridge.emit(["type": "ready"])
    }

    private func listenStdin() {
        let handle = FileHandle.standardInput
        let source = DispatchSource.makeReadSource(fileDescriptor: handle.fileDescriptor, queue: .main)
        var leftover = Data()
        source.setEventHandler { [weak self] in
            let chunk = handle.availableData
            if chunk.isEmpty {
                NSApp.terminate(nil)
                return
            }
            leftover.append(chunk)
            while let range = leftover.range(of: Data([0x0A])) {
                let line = leftover.subdata(in: leftover.startIndex..<range.lowerBound)
                leftover.removeSubrange(leftover.startIndex...range.lowerBound)
                self?.handle(line: line)
            }
        }
        source.resume()
        stdin = source
    }

    private func handle(line: Data) {
        guard
            let object = try? JSONSerialization.jsonObject(with: line) as? [String: Any],
            let type = object["type"] as? String
        else { return }
        if type == "quit" {
            session?.requestEnd()
            LocalServiceStarter.shared.stop()
            NSApp.terminate(nil)
            return
        }
        if type == "speak", let text = object["text"] as? String {
            session?.speak(text)
        }
    }

    private static func emit(_ object: [String: Any]) {
        guard let data = try? JSONSerialization.data(withJSONObject: object),
              let line = String(data: data, encoding: .utf8) else { return }
        FileHandle.standardOutput.write(Data((line + "\n").utf8))
    }
}
