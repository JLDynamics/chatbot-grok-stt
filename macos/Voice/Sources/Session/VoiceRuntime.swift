import Foundation

/// Invalidates asynchronous work when a connection or user turn is superseded.
@MainActor
final class VoiceWorkScope {
    private(set) var generation = UUID()
    private var tasks: [String: Task<Void, Never>] = [:]

    var pendingIds: [String] { Array(tasks.keys) }
    func contains(_ id: String) -> Bool { tasks[id] != nil }
    func insert(_ task: Task<Void, Never>, id: String) { tasks[id] = task }
    func finish(_ id: String, generation: UUID) {
        guard self.generation == generation else { return }
        tasks[id] = nil
    }
    func cancel() {
        generation = UUID()
        let obsolete = tasks.values
        tasks.removeAll()
        for task in obsolete { task.cancel() }
    }
}

/// Playback completions from an old queue cannot consume a new queue's buffers.
final class PlaybackTracker: @unchecked Sendable {
    private let lock = NSLock()
    private var generation = UUID()
    private var count = 0
    private var drainedAt = Date.distantPast

    var isAudible: Bool {
        lock.lock(); defer { lock.unlock() }
        return count > 0
    }
    func needsEchoGuard(now: Date = Date()) -> Bool {
        lock.lock(); defer { lock.unlock() }
        return count > 0 || now.timeIntervalSince(drainedAt) < 0.9
    }
    func enqueue() -> UUID {
        lock.lock(); defer { lock.unlock() }
        count += 1
        return generation
    }
    @discardableResult
    func complete(_ token: UUID, now: Date = Date()) -> Bool {
        lock.lock(); defer { lock.unlock() }
        guard token == generation, count > 0 else { return false }
        count -= 1
        if count == 0 { drainedAt = now }
        return count == 0
    }
    func clear(now: Date = Date()) {
        lock.lock(); defer { lock.unlock() }
        if count > 0 { drainedAt = now }
        generation = UUID()
        count = 0
    }
}

/// Keep transport metadata with extracted content, including unsuccessful reads.
enum VoiceToolFormatting {
    static func page(_ json: [String: Any], source: String) -> String {
        var result = json
        result["source"] = source
        let text = (json["text"] as? String ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        let gated = json["gated"] as? Bool ?? false
        result["status"] = gated ? "blocked" : (json["status"] as? String ?? (text.isEmpty ? "empty" : "read"))
        result["complete"] = !gated && !text.isEmpty
            && !(json["truncated"] as? Bool ?? false) && (json["complete"] as? Bool ?? true)
        guard let data = try? JSONSerialization.data(withJSONObject: result, options: [.sortedKeys]),
              let output = String(data: data, encoding: .utf8) else { return "Invalid page result." }
        return output
    }
}
