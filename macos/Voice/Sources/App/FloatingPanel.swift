import AppKit

/// The window itself. An NSPanel, not an NSWindow, so that clicking a control
/// inside it does not pull focus away from whatever app you were working in.
///
/// A panel cannot minimize to the Dock — that is deliberate on Apple's part and
/// this app hides and re-summons instead. See SPEC.md section 2.
final class FloatingPanel: NSPanel {

    static let defaultSize = NSSize(width: 360, height: 460)
    static let minimumSize = NSSize(width: 320, height: 380)

    init(contentRect: NSRect = NSRect(origin: .zero, size: FloatingPanel.defaultSize)) {
        super.init(
            contentRect: contentRect,
            styleMask: [.nonactivatingPanel, .titled, .fullSizeContentView, .resizable, .closable],
            backing: .buffered,
            defer: false
        )

        // Floating behaviour
        isFloatingPanel = true
        becomesKeyOnlyIfNeeded = true       // a click on a button does not steal focus
        hidesOnDeactivate = false           // stays put when you switch apps
        level = .floating
        collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]

        // Chrome: we draw our own header (with dedicated WindowDragArea), so hide Apple's
        isMovableByWindowBackground = false
        titlebarAppearsTransparent = true
        titleVisibility = .hidden
        isOpaque = false
        backgroundColor = .clear
        standardWindowButton(.closeButton)?.isHidden = true
        standardWindowButton(.miniaturizeButton)?.isHidden = true
        standardWindowButton(.zoomButton)?.isHidden = true

        contentMinSize = FloatingPanel.minimumSize
        setFrameAutosaveName("VoicePanel")  // position and size persist across launches
        isReleasedWhenClosed = false
    }

    // A panel is not key by default; without this the keyboard shortcuts in
    // SPEC.md section 9 never fire.
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { false }

    /// Flipped by the header's "On top" toggle.
    func setAlwaysOnTop(_ onTop: Bool) {
        level = onTop ? .floating : .normal
    }
}
