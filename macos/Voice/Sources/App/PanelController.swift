import AppKit
import SwiftUI

/// Hosting view that accepts the first mouse event so controls click immediately
/// even when the nonactivating floating panel is not currently the key window.
final class FirstMouseHostingView<Content: View>: NSHostingView<Content> {
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool {
        true
    }
}

/// Owns the panel and the SwiftUI view inside it. One instance, held by the app
/// delegate for the lifetime of the process — the panel is hidden and shown, not
/// created and destroyed, so its position survives.
@MainActor
final class PanelController<Root: View>: NSObject, NSWindowDelegate {

    private let panel: FloatingPanel
    private let onHide: (() -> Void)?

    /// Held for the length of a resize drag. The panel is nonactivating, so
    /// during a drag this process is usually *not* the frontmost app and is a
    /// candidate for App Nap's timer coalescing and lowered CPU priority —
    /// which is felt directly as the window edge lagging the cursor.
    private var resizeActivity: NSObjectProtocol?

    /// - Parameter onHide: called whenever the panel is hidden. Wire this to end
    ///   any live session — a hot mic behind a hidden window is the one bug in
    ///   this app that is genuinely unacceptable.
    init(root: Root, onHide: (() -> Void)? = nil) {
        self.panel = FloatingPanel()
        self.onHide = onHide
        super.init()

        let hosting = FirstMouseHostingView(rootView: root)
        hosting.autoresizingMask = [.width, .height]
        // The hosting view is sized by the autoresizing mask above and the
        // panel's own contentMinSize, so it does not need to publish SwiftUI's
        // measured sizes back as Auto Layout constraints. Leaving that on costs
        // a constraint solve per resize frame for no benefit.
        hosting.sizingOptions = []
        panel.contentView = hosting
        panel.delegate = self

        // Corner rounding is done by the backing layer, not by a SwiftUI
        // clipShape on the root. A clipShape wrapping the whole tree forces the
        // entire window to be rendered offscreen and masked on every frame of a
        // resize drag; a layer corner radius is a single GPU operation and looks
        // identical. The panel behind the layer is clear.
        hosting.wantsLayer = true
        hosting.layer?.cornerRadius = Theme.radius
        hosting.layer?.masksToBounds = true
    }

    var isVisible: Bool { panel.isVisible }

    func show() {
        if panel.frame.origin == .zero { panel.center() }
        panel.orderFrontRegardless()
        panel.makeKey()
    }

    func hide() {
        panel.savePersistedFrame()
        panel.orderOut(nil)
        onHide?()
    }

    func toggle() {
        isVisible ? hide() : show()
    }

    func setAlwaysOnTop(_ onTop: Bool) {
        panel.setAlwaysOnTop(onTop)
    }

    func windowWillStartLiveResize(_ notification: Notification) {
        resizeActivity = ProcessInfo.processInfo.beginActivity(
            options: [.userInitiated, .latencyCritical],
            reason: "Panel live resize"
        )
        LiveResizeMonitor.shared.begin()
    }

    func windowDidEndLiveResize(_ notification: Notification) {
        LiveResizeMonitor.shared.end()
        if let resizeActivity {
            ProcessInfo.processInfo.endActivity(resizeActivity)
            self.resizeActivity = nil
        }
        panel.savePersistedFrame()
    }

    // Close ends up here because the panel is never released.
    func windowShouldClose(_ sender: NSWindow) -> Bool {
        hide()
        return false
    }
}
