import Foundation

/// Single source of truth for local service URLs.
///
/// The native panel talks to two local servers: the realtime voice backend
/// (WebSocket) and the FastAPI sidecar (HTTP `/api/*` for sessions, memory,
/// tools, and the Chrome bridge receiver).
public enum LocalService {
    public static let sidecarAPI = URL(string: "http://127.0.0.1:7860/api")!
    public static let voiceWebSocket = URL(string: "ws://127.0.0.1:8766/v1/realtime")!
}
