import Foundation

/// Single source of truth for local service URLs.
///
/// The native panel talks to two local servers: the realtime voice backend
/// (WebSocket) and the FastAPI sidecar (HTTP `/api/*` for sessions, memory,
/// tools, and the Chrome bridge receiver).
///
/// Both default to the ports `run-browser.sh` uses and can be pointed
/// elsewhere with a user default, so the panel can be run against a second
/// stack on offset ports without disturbing the everyday one:
///
///     defaults write com.jack.Voice voice.wsUrl "ws://127.0.0.1:8866/v1/realtime"
///     defaults write com.jack.Voice voice.sidecarUrl "http://127.0.0.1:7960/api"
public enum LocalService {
    static let defaultSidecarAPI = "http://127.0.0.1:7860/api"
    static let defaultVoiceWebSocket = "ws://127.0.0.1:8766/v1/realtime"

    private static func url(overrideKey: String, fallback: String) -> URL {
        guard let raw = UserDefaults.standard.string(forKey: overrideKey)?
            .trimmingCharacters(in: .whitespacesAndNewlines),
            !raw.isEmpty,
            let overridden = URL(string: raw)
        else {
            return URL(string: fallback)!
        }
        return overridden
    }

    public static var sidecarAPI: URL {
        url(overrideKey: "voice.sidecarUrl", fallback: defaultSidecarAPI)
    }

    public static var voiceWebSocket: URL {
        url(overrideKey: "voice.wsUrl", fallback: defaultVoiceWebSocket)
    }
}
