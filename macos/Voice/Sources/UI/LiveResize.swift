import SwiftUI

/// True while the user is dragging a resize edge of the panel.
///
/// AppKit drives live resize synchronously: on every mouse-moved frame the
/// window server waits for our content to redraw before it moves the window
/// edge. Anything expensive in the SwiftUI tree — behind-window blur,
/// selectable text, gradients — is paid at that cadence, and the window
/// visibly trails the cursor. Views read this flag and drop to a cheaper
/// rendering for the length of the drag; nothing about the layout changes, so
/// there is no visible jump when it clears.
private struct LiveResizeKey: EnvironmentKey {
    static let defaultValue = false
}

extension EnvironmentValues {
    var isLiveResizing: Bool {
        get { self[LiveResizeKey.self] }
        set { self[LiveResizeKey.self] = newValue }
    }
}

/// Publishes the flag above. One instance, flipped by the panel's window
/// delegate — twice per drag, not once per frame.
@MainActor
final class LiveResizeMonitor: ObservableObject {
    static let shared = LiveResizeMonitor()

    @Published private(set) var isResizing = false

    private init() {}

    func begin() {
        guard !isResizing else { return }
        isResizing = true
    }

    func end() {
        guard isResizing else { return }
        isResizing = false
    }
}
