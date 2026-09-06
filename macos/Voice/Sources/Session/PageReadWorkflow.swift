import Foundation

/// A bounded text-reading workflow. Every attempted method and failure is retained.
/// A requested URL must never silently turn into a different open Chrome page.
struct PageReadWorkflow {
    typealias Attempt = (String, String?) async throws -> [String: Any]

    private static let browserFirstHosts: Set<String> = [
        "x.com", "www.x.com", "mobile.x.com", "twitter.com", "www.twitter.com", "mobile.twitter.com"
    ]

    static func prefersBrowser(_ url: String) -> Bool {
        guard let host = URLComponents(string: url)?.host?.lowercased() else { return false }
        return browserFirstHosts.contains(host)
    }

    static func samePage(_ left: String, _ right: String) -> Bool {
        func normalized(_ raw: String) -> String? {
            guard var url = URLComponents(string: raw),
                  let scheme = url.scheme?.lowercased(), ["http", "https"].contains(scheme),
                  let host = url.host else { return nil }
            url.scheme = scheme
            url.host = browserFirstHosts.contains(host.lowercased()) ? "x.com" : host.lowercased()
            url.fragment = nil
            if url.path.isEmpty { url.path = "/" }
            return url.string
        }
        guard let a = normalized(left), let b = normalized(right) else { return false }
        return a == b
    }

    static func read(url: String?, allowFetch: Bool, allowBridge: Bool, preferBrowser: Bool = false,
                     attempt: Attempt) async throws -> [String: Any] {
        var attempts = [[String: Any]]()
        var knownURL = url
        var partial: [String: Any]?
        let browserFirst = preferBrowser || (url.map(prefersBrowser) ?? true)
        let methods = browserFirst ? ["chrome_bridge", "web_fetch"] : ["web_fetch", "chrome_bridge"]
        for method in methods {
            try Task.checkCancellation()
            if method == "web_fetch" && (!allowFetch || knownURL == nil) { continue }
            if method == "chrome_bridge" && !allowBridge { continue }
            var result = try await attempt(method, knownURL)
            try Task.checkCancellation()
            if let requested = url, method == "chrome_bridge",
               !(result["text"] as? String ?? "").trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
               !samePage(requested, result["url"] as? String ?? "") {
                result = ["status": "wrong_page", "message": "Chrome has a different page open."]
            }
            if knownURL == nil, let address = result["url"] as? String { knownURL = address }
            let text = (result["text"] as? String ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            let status = result["status"] as? String ?? ""
            let usable = !text.isEmpty && !(result["gated"] as? Bool ?? false)
                && !["wrong_page", "failed", "blocked", "unavailable", "error"].contains(status)
            let complete = usable && !(result["truncated"] as? Bool ?? false)
                && (result["complete"] as? Bool ?? true)
            attempts.append(["method": method, "status": usable ? (complete ? "read" : "partial") : (result["status"] as? String ?? "failed"),
                             "reason": result["reason"] ?? result["gated_reason"] ?? result["message"] ?? ""])
            if usable {
                result["source"] = method
                result["complete"] = complete
                if complete {
                    result["attempts"] = attempts
                    return result
                }
                // Keep useful partial text while trying the other method for a complete page.
                // Do not concatenate sources: that can duplicate text or mix page revisions.
                if partial == nil { partial = result }
            }
        }
        if var partial {
            partial["status"] = "partial"
            partial["attempts"] = attempts
            return partial
        }
        var failure: [String: Any] = ["status": "unavailable", "complete": false, "attempts": attempts,
            "message": "Text methods did not return the requested page. A screenshot is only useful if that page is visible; it is not a full-article result."]
        if let knownURL { failure["url"] = knownURL }
        return failure
    }
}

/// Screen reading cannot loop forever when scrolling does not move the page.
struct ScreenReadBudget {
    private var signatures = Set<String>()
    private var captures = 0
    private(set) var stopped = false
    mutating func accept(signature: String) -> Bool {
        guard !stopped, captures < 8, signatures.insert(signature).inserted else { stopped = true; return false }
        captures += 1
        if captures == 8 { stopped = true }
        return true
    }
}
