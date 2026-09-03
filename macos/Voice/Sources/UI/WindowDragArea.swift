import AppKit
import SwiftUI

/// Drag region for the floating panel. SwiftUI hosting views eat mouse events,
/// so `isMovableByWindowBackground` alone does not let you drag the window.
struct WindowDragArea: NSViewRepresentable {
    func makeNSView(context: Context) -> NSView {
        DragNSView()
    }

    func updateNSView(_ nsView: NSView, context: Context) {}

    private final class DragNSView: NSView {
        override var mouseDownCanMoveWindow: Bool { true }

        override func mouseDown(with event: NSEvent) {
            window?.performDrag(with: event)
        }
    }
}
