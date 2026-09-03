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

    /// - Parameter onHide: called whenever the panel is hidden. Wire this to end
    ///   any live session — a hot mic behind a hidden window is the one bug in
    ///   this app that is genuinely unacceptable.
    init(root: Root, onHide: (() -> Void)? = nil) {
        self.panel = FloatingPanel()
        self.onHide = onHide
        super.init()

        let hosting = FirstMouseHostingView(rootView: root)
        hosting.autoresizingMask = [.width, .height]
        panel.contentView = hosting
        panel.delegate = self

        // Corner rounding lives on the SwiftUI root; the panel behind it is clear.
        panel.contentView?.wantsLayer = true
    }

    var isVisible: Bool { panel.isVisible }

    func show() {
        if panel.frame.origin == .zero { panel.center() }
        panel.orderFrontRegardless()
        panel.makeKey()
    }

    func hide() {
        panel.orderOut(nil)
        onHide?()
    }

    func toggle() {
        isVisible ? hide() : show()
    }

    func setAlwaysOnTop(_ onTop: Bool) {
        panel.setAlwaysOnTop(onTop)
    }

    // Close ends up here because the panel is never released.
    func windowShouldClose(_ sender: NSWindow) -> Bool {
        hide()
        return false
    }
}
