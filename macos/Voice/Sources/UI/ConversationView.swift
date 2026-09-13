import AppKit
import SwiftUI

struct ConversationView: View {
    @ObservedObject var session: SessionController
    @Binding var alwaysOnTop: Bool
    @Binding var themePreference: ThemePreference
    var onPinToggle: (Bool) -> Void
    var onClose: () -> Void

    @Environment(\.colorScheme) private var colorScheme
    @ObservedObject private var liveResize = LiveResizeMonitor.shared
    @State private var showJumpToLatest = false
    @State private var stickToBottom = true

    private var resolvedTheme: Theme {
        switch themePreference {
        case .light: return .light
        case .dark: return .dark
        case .auto: return colorScheme == .dark ? .dark : .light
        }
    }

    var body: some View {
        VStack(spacing: 0) {
            HeaderView(
                alwaysOnTop: $alwaysOnTop,
                themePreference: $themePreference,
                errorText: session.errorText,
                onPinToggle: { onPinToggle(alwaysOnTop) },
                onSettingsToggle: {
                    session.showSessions = false
                    session.showSettings.toggle()
                },
                onHistoryToggle: {
                    session.showSettings = false
                    session.showSessions.toggle()
                },
                onClose: onClose
            )

            TranscriptView(
                session: session,
                showJumpToLatest: $showJumpToLatest,
                stickToBottom: $stickToBottom,
                onJumpToLatest: {
                    showJumpToLatest = false
                    stickToBottom = true
                }
            )
            .frame(maxHeight: .infinity)

            // Fixed chrome so Listening/Thinking/tool pills cannot resize
            // the transcript and bounce its scroller.
            conversationChrome

            LevelMeterView(
                state: session.state,
                isMuted: session.isMuted,
                levels: session.levels
            )
            .padding(.horizontal, 16)

            ControlBar(session: session, onOrbTap: { session.toggleSession() })
        }
        .environment(\.theme, resolvedTheme)
        .environment(\.isLiveResizing, liveResize.isResizing)
        .background(panelBackground)
        // No clipShape here: the hosting view's layer rounds the corners, so
        // the tree is not re-masked offscreen on every resize frame. The border
        // below is a single stroked outline and stays in SwiftUI, because its
        // colour follows the theme.
        .overlay(
            RoundedRectangle(cornerRadius: Theme.radius)
                .strokeBorder(resolvedTheme.border, lineWidth: 0.5)
        )
        .overlay {
            if session.showSettings {
                SettingsView(session: session, onClose: { session.showSettings = false })
                    .transition(.opacity.combined(with: .move(edge: .top)))
            } else if session.showSessions {
                SessionsView(session: session, onClose: { session.showSessions = false })
                    .transition(.opacity.combined(with: .move(edge: .top)))
            }
        }
        .animation(.easeOut(duration: 0.12), value: session.showSettings)
        .animation(.easeOut(duration: 0.12), value: session.showSessions)
        .preferredColorScheme(themePreference == .auto ? nil : (themePreference == .dark ? .dark : .light))
        .onChange(of: session.state) { _, newState in
            announceState(newState)
        }
    }

    private var canStopReply: Bool {
        session.state == .agentSpeaking || session.state == .thinking || session.activeTool != nil
    }

    private var conversationChrome: some View {
        VStack(spacing: 2) {
            HStack(spacing: 8) {
                ZStack {
                    if let tool = session.activeTool {
                        HStack(spacing: 6) {
                            ProgressView()
                                .controlSize(.small)
                            Text(tool)
                                .font(.system(size: 11.5, weight: .medium))
                                .lineLimit(1)
                        }
                        .foregroundStyle(resolvedTheme.accent)
                        .padding(.horizontal, 10)
                        .padding(.vertical, 4)
                        .background(resolvedTheme.surface2)
                        .clipShape(Capsule())
                    }
                }
                .frame(maxWidth: .infinity, alignment: .center)

                Button("Stop reply") { session.interrupt() }
                    .buttonStyle(PressableButtonStyle())
                    .font(.system(size: 11, weight: .medium))
                    .opacity(canStopReply ? 1 : 0)
                    .allowsHitTesting(canStopReply)
                    .accessibilityHidden(!canStopReply)
                    .accessibilityIdentifier("voice.stopReply")
            }
            .frame(height: 28)
            .padding(.horizontal, 12)

            if let note = session.audioStatus {
                Text(note)
                    .font(.system(size: 10))
                    .foregroundStyle(resolvedTheme.text3)
                    .padding(.horizontal, 16)
            }
        }
        .padding(.bottom, 2)
    }

    private func announceState(_ state: SessionState) {
        let message: String?
        switch state {
        case .idle: message = "Session ended"
        case .connecting: message = "Connecting"
        case .failed: message = "Session failed"
        default: return
        }
        if let message {
            NSAccessibility.post(element: NSApp as Any, notification: .announcementRequested, userInfo: [
                .announcement: message
            ])
        }
    }

    /// Behind-window blending samples whatever is under the panel and reblurs
    /// it every frame — the single most expensive thing in the tree while a
    /// resize edge is being dragged. The opaque fill underneath already carries
    /// most of the panel's colour, so dropping the blur for the length of the
    /// drag is close to invisible and buys back the frame budget.
    @ViewBuilder
    private var panelBackground: some View {
        ZStack {
            if !liveResize.isResizing {
                VisualEffectBlur(material: .hudWindow, blendingMode: .behindWindow)
            }
            resolvedTheme.bg.opacity(liveResize.isResizing ? 1 : 0.92)
        }
    }
}

/// NSVisualEffectView wrapper for the HUD material behind the panel.
struct VisualEffectBlur: NSViewRepresentable {
    var material: NSVisualEffectView.Material
    var blendingMode: NSVisualEffectView.BlendingMode

    func makeNSView(context: Context) -> NSVisualEffectView {
        let view = NSVisualEffectView()
        view.material = material
        view.blendingMode = blendingMode
        view.state = .active
        return view
    }

    func updateNSView(_ nsView: NSVisualEffectView, context: Context) {
        // Assigning these unconditionally makes the effect view reconfigure and
        // re-sample on every SwiftUI update pass, even when nothing changed.
        if nsView.material != material {
            nsView.material = material
        }
        if nsView.blendingMode != blendingMode {
            nsView.blendingMode = blendingMode
        }
    }
}
