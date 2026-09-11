import Foundation

/// Saved conversation + personal memory against `web_app/server.py`'s
/// `/api` endpoints. The realtime voice backend itself is stateless per
/// connection; continuity comes from replaying saved messages into the
/// WebSocket and PATCHing turns back.
public struct ChatMessage: Codable, Equatable {
    public var role: String   // "user" | "assistant" | "tool"
    public var text: String
    public var name: String?  // tool name when role == "tool"

    public init(role: String, text: String, name: String? = nil) {
        self.role = role
        self.text = text
        self.name = name
    }
}

public struct ChatSession: Codable {
    public var id: String
    public var title: String
    public var messages: [ChatMessage]

    public init(id: String, title: String, messages: [ChatMessage] = []) {
        self.id = id
        self.title = title
        self.messages = messages
    }
}

public struct ChatSessionSummary: Codable, Identifiable {
    public var id: String
    public var title: String
    public var preview: String?
    public var message_count: Int?
    public var updated_at: String?

    public init(id: String, title: String, preview: String? = nil,
                message_count: Int? = nil, updated_at: String? = nil) {
        self.id = id
        self.title = title
        self.preview = preview
        self.message_count = message_count
        self.updated_at = updated_at
    }
}

public struct SidecarConfig: Codable, Equatable {
    public var search: Bool
    public var desktopControl: Bool

    public init(search: Bool = false, desktopControl: Bool = false) {
        self.search = search
        self.desktopControl = desktopControl
    }
}

struct ChatStoreError: LocalizedError {
    let status: Int
    let detail: String?
    var errorDescription: String? { detail ?? "Server error (\(status))" }

    static func from(status: Int, data: Data) -> ChatStoreError {
        let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        let detail = json?["detail"] as? String
        return ChatStoreError(status: status, detail: detail)
    }
}

/// Thin async client for the local FastAPI sidecar (default
/// `http://127.0.0.1:7860/api`, same base URL as VoiceToolExecutor).
public final class ChatStore: @unchecked Sendable {
    public static let shared = ChatStore()

    private let baseURL = LocalService.sidecarAPI
    private let session: URLSession = {
        let cfg = URLSessionConfiguration.default
        cfg.timeoutIntervalForRequest = 8
        cfg.timeoutIntervalForResource = 15
        return URLSession(configuration: cfg)
    }()

    private struct SessionsList: Codable { var sessions: [ChatSessionSummary] }
    private struct SessionEnvelope: Codable { var session: ChatSession }
    private struct MemoryEnvelope: Codable { var content: String; var max_chars: Int? }

    // MARK: - Sessions

    public func listSessions() async throws -> [ChatSessionSummary] {
        let data = try await get("sessions")
        return try JSONDecoder().decode(SessionsList.self, from: data).sessions
    }

    public func createSession(title: String = "New conversation") async throws -> ChatSession {
        let data = try await post("sessions", body: ["title": title])
        return try JSONDecoder().decode(SessionEnvelope.self, from: data).session
    }

    public func getSession(id: String) async throws -> ChatSession {
        let data = try await get("sessions/\(id)")
        return try JSONDecoder().decode(SessionEnvelope.self, from: data).session
    }

    public func patchSession(id: String, title: String, messages: [ChatMessage]) async throws {
        let body: [String: Any] = [
            "title": title,
            "messages": messages.map { m -> [String: String] in
                var d = ["role": m.role, "text": m.text]
                if let n = m.name { d["name"] = n }
                return d
            },
        ]
        _ = try await patch("sessions/\(id)", body: body)
    }

    public func deleteSession(id: String) async throws {
        var req = URLRequest(url: baseURL.appendingPathComponent("sessions/\(id)"))
        req.httpMethod = "DELETE"
        let (data, res) = try await session.data(for: req)
        try check(data, res)
    }

    // MARK: - Personal memory

    public func getPersonalMemory() async throws -> String {
        let data = try await get("personal-memory")
        return try JSONDecoder().decode(MemoryEnvelope.self, from: data).content
    }

    public func putPersonalMemory(content: String) async throws -> String {
        let data = try await put("personal-memory", body: ["content": content])
        return try JSONDecoder().decode(MemoryEnvelope.self, from: data).content
    }

    public func getConfig() async throws -> SidecarConfig {
        let data = try await get("config")
        return try JSONDecoder().decode(SidecarConfig.self, from: data)
    }

    // MARK: - History search (for the search_chat_history tool)

    public func searchHistory(query: String) async throws -> String {
        var comps = URLComponents(url: baseURL.appendingPathComponent("history/search"),
                                  resolvingAgainstBaseURL: false)!
        comps.queryItems = [URLQueryItem(name: "q", value: query),
                            URLQueryItem(name: "limit", value: "8")]
        let (data, res) = try await session.data(from: comps.url!)
        guard let http = res as? HTTPURLResponse, http.statusCode == 200,
              let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let results = json["results"] as? [[String: Any]], !results.isEmpty
        else { return "No matching saved conversation was found." }
        return results.map { item in
            let title = item["title"] as? String ?? ""
            let role = item["role"] as? String ?? ""
            let text = item["text"] as? String ?? ""
            return "[\(title)] \(role): \(text)"
        }.joined(separator: "\n\n")
    }

    // MARK: - Plumbing

    private func check(_ data: Data, _ res: URLResponse) throws {
        guard let http = res as? HTTPURLResponse else {
            throw URLError(.badServerResponse)
        }
        guard http.statusCode == 200 else {
            throw ChatStoreError.from(status: http.statusCode, data: data)
        }
    }

    private func get(_ path: String) async throws -> Data {
        let (data, res) = try await session.data(from: baseURL.appendingPathComponent(path))
        try check(data, res)
        return data
    }

    private func post(_ path: String, body: [String: Any]) async throws -> Data {
        try await send(path, method: "POST", body: body)
    }

    private func put(_ path: String, body: [String: Any]) async throws -> Data {
        try await send(path, method: "PUT", body: body)
    }

    private func patch(_ path: String, body: [String: Any]) async throws -> Data {
        try await send(path, method: "PATCH", body: body)
    }

    private func send(_ path: String, method: String, body: [String: Any]) async throws -> Data {
        var req = URLRequest(url: baseURL.appendingPathComponent(path))
        req.httpMethod = method
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try JSONSerialization.data(withJSONObject: body)
        let (data, res) = try await session.data(for: req)
        try check(data, res)
        return data
    }
}
