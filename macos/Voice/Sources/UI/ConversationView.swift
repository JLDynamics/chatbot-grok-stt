import AppKit
import SwiftUI

struct ConversationView: View {
    @ObservedObject var session: SessionController
    @Binding var alwaysOnTop: Bool
    @Binding var themePreference: ThemePreference
    var onPinToggle: (Bool) -> Void
    var onClose: () -> Void

    @Environment(\.colorScheme) private var colorScheme
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
                onJumpToLatest: {
                    showJumpToLatest = false
                    stickToBottom = true
                }
            )
            .frame(maxHeight: .infinity)

            if let tool = session.activeTool {
                HStack(spacing: 6) {
                    ProgressView()
                        .controlSize(.small)
                    Text(tool)
                        .font(.system(size: 11.5, weight: .medium))
                }
                .foregroundStyle(resolvedTheme.accent)
                .padding(.horizontal, 10)
                .padding(.vertical, 4)
                .background(resolvedTheme.surface2)
                .clipShape(Capsule())
                .padding(.bottom, 6)
                .transition(.opacity.combined(with: .scale))
            }

            if let note = session.audioStatus {
                Text(note).font(.system(size: 10)).foregroundStyle(resolvedTheme.text3)
                    .padding(.horizontal, 16)
            }
            if session.state == .agentSpeaking || session.state == .thinking || session.activeTool != nil {
                Button("Stop reply") { session.interrupt() }
                    .buttonStyle(PressableButtonStyle())
                    .font(.system(size: 11, weight: .medium))
                    .padding(.vertical, 4)
            }

            LevelMeterView(
                state: session.state,
                isMuted: session.isMuted,
                levels: session.levels
            )
            .padding(.horizontal, 16)

            ControlBar(session: session, onOrbTap: { session.toggleSession() })
        }
        .environment(\.theme, resolvedTheme)
        .background(panelBackground)
        .clipShape(RoundedRectangle(cornerRadius: Theme.radius))
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
        .animation(.easeOut(duration: 0.12), value: session.activeTool)
        .preferredColorScheme(themePreference == .auto ? nil : (themePreference == .dark ? .dark : .light))
        .onChange(of: session.state) { _, newState in
            announceState(newState)
        }
    }

    private func announceState(_ state: SessionState) {
        let message: String?
        switch state {
        case .idle: message = "Session ended"
        case .connecting: message = "Connecting"
        case .listening: message = session.isMuted ? "Muted" : "Listening"
        case .thinking: message = "Thinking"
        case .agentSpeaking: message = "Agent speaking"
        case .failed: message = nil
        }
        if let message {
            NSAccessibility.post(element: NSApp as Any, notification: .announcementRequested, userInfo: [
                .announcement: message
            ])
        }
    }

    @ViewBuilder
    private var panelBackground: some View {
        ZStack {
            VisualEffectBlur(material: .hudWindow, blendingMode: .behindWindow)
            resolvedTheme.bg.opacity(0.92)
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
        nsView.material = material
        nsView.blendingMode = blendingMode
    }
}
