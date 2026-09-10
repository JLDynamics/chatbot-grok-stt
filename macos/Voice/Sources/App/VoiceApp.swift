import AppKit
import SwiftUI

@main
struct VoiceApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate

    var body: some Scene {
        MenuBarExtra("Voice", systemImage: "waveform.circle") {
            MenuBarMenu(appDelegate: appDelegate)
        }
        .menuBarExtraStyle(.menu)
    }
}

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate, ObservableObject {
    static weak var shared: AppDelegate?

    let session: SessionController
    private var panelController: PanelController<RootView>?
    private var hotKey: GlobalHotKey?
    private var escMonitor: Any?

    private static let defaultWSURL = LocalService.voiceWebSocket

    private static func makeBackend() -> VoiceBackend {
        if UserDefaults.standard.bool(forKey: "voice.useMock") {
            return MockVoiceBackend()
        }
        let raw = UserDefaults.standard.string(forKey: "voice.wsUrl") ?? defaultWSURL.absoluteString
        let url = URL(string: raw) ?? defaultWSURL
        return LiveVoiceBackend(url: url)
    }

    override init() {
        self.session = SessionController(backend: AppDelegate.makeBackend())
        self.alwaysOnTop = UserDefaults.standard.bool(forKey: "alwaysOnTop")
        self.themePreference = ThemePreference(
            rawValue: UserDefaults.standard.string(forKey: "themePreference") ?? ""
        ) ?? .auto
        super.init()
    }

    @Published var alwaysOnTop: Bool {
        didSet {
            UserDefaults.standard.set(alwaysOnTop, forKey: "alwaysOnTop")
            panelController?.setAlwaysOnTop(alwaysOnTop)
        }
    }

    @Published var themePreference: ThemePreference {
        didSet {
            UserDefaults.standard.set(themePreference.rawValue, forKey: "themePreference")
        }
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        Self.shared = self
        NSApp.setActivationPolicy(.regular)

        let root = RootView(
            session: session,
            appDelegate: self,
            onPinToggle: { [weak self] on in self?.setAlwaysOnTop(on) },
            onClose: { [weak self] in self?.hidePanel() }
        )

        panelController = PanelController(root: root) { [weak self] in
            self?.session.requestEnd()
        }
        panelController?.setAlwaysOnTop(alwaysOnTop)

        hotKey = GlobalHotKey { [weak self] in
            Task { @MainActor in
                self?.showPanelAndStart()
            }
        }

        escMonitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] event in
            guard let self else { return event }
            let flags = event.modifierFlags.intersection(.deviceIndependentFlagsMask)
            if flags.contains(.command),
               event.charactersIgnoringModifiers?.lowercased() == "w" {
                self.hidePanel()
                return nil
            }
            // Text editors own ordinary typing; space and M must not toggle
            // the conversation or microphone while editing personal memory.
            if let editor = NSApp.keyWindow?.firstResponder as? NSTextView, editor.isEditable {
                return event
            }
            // Space toggles session when panel is key (SPEC §9).
            if event.keyCode == 49, flags.isEmpty, !event.isARepeat {
                self.session.toggleSession()
                return nil
            }
            // M toggles mute.
            if event.charactersIgnoringModifiers?.lowercased() == "m", flags.isEmpty {
                self.session.toggleMute()
                return nil
            }
            guard event.keyCode == 53 else { return event } // Esc
            if self.session.isLive {
                self.session.requestEnd()
                return nil
            }
            if self.panelController?.isVisible == true {
                self.hidePanel()
                return nil
            }
            return event
        }

        showPanel()
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        showPanel()
        return true
    }

    func applicationWillTerminate(_ notification: Notification) {
        LocalServiceStarter.shared.stop()
        Task { await session.end() }
    }

    func showPanel() {
        panelController?.setAlwaysOnTop(alwaysOnTop)
        panelController?.show()
    }

    func hidePanel() {
        session.requestEnd()
        panelController?.hide()
    }

    func togglePanel() {
        if panelController?.isVisible == true {
            hidePanel()
        } else {
            showPanel()
        }
    }

    func setAlwaysOnTop(_ on: Bool) {
        alwaysOnTop = on
        panelController?.setAlwaysOnTop(on)
    }

    func showPanelAndStart() {
        showPanel()
        if !session.isLive {
            session.toggleSession()
        }
    }
}

private struct RootView: View {
    @ObservedObject var session: SessionController
    @ObservedObject var appDelegate: AppDelegate
    var onPinToggle: (Bool) -> Void
    var onClose: () -> Void

    var body: some View {
        ConversationView(
            session: session,
            alwaysOnTop: $appDelegate.alwaysOnTop,
            themePreference: $appDelegate.themePreference,
            onPinToggle: onPinToggle,
            onClose: onClose
        )
        .frame(
            minWidth: FloatingPanel.minimumSize.width,
            minHeight: FloatingPanel.minimumSize.height
        )
    }
}

private struct MenuBarMenu: View {
    @ObservedObject var appDelegate: AppDelegate

    var body: some View {
        Button(appDelegate.session.isLive ? "End session" : "Start session") {
            appDelegate.session.toggleSession()
        }
        Button("Show panel") { appDelegate.showPanel() }
        Button("Hide panel") { appDelegate.hidePanel() }
        Divider()
        Button("Quit Voice") { NSApp.terminate(nil) }
    }
}
