import SwiftUI

struct ControlBar: View {
    @ObservedObject var session: SessionController
    var onOrbTap: () -> Void

    @Environment(\.theme) private var theme

  private var compact: Bool {
        // Below 340 pt width, shrink orb and side buttons per SPEC §3.
        false // GeometryReader could drive this; default sizes for now.
    }

    private var sideSize: CGFloat { compact ? 40 : 44 }
    private var orbSize: CGFloat { compact ? 56 : 64 }

    var body: some View {
        HStack(spacing: 28) {
            muteButton
            OrbView(
                state: session.state,
                isMuted: session.isMuted,
                inputLevel: session.inputLevel,
                onTap: onOrbTap
            )
            .frame(width: orbSize, height: orbSize)
            stopButton
        }
        .frame(height: 92)
        .frame(maxWidth: .infinity)
    }

    private var muteButton: some View {
        Button(action: { session.toggleMute() }) {
            Image(systemName: session.isMuted ? "mic.slash" : "mic")
                .font(.system(size: 20))
                .foregroundStyle(session.isMuted ? theme.text : theme.text3)
                .frame(width: sideSize, height: sideSize)
                .background(Color.clear)
                .overlay(
                    Circle()
                        .strokeBorder(session.isMuted ? theme.borderStrong : theme.border, lineWidth: 0.5)
                )
        }
        .buttonStyle(HoverButtonStyle())
        .disabled(!session.isLive || session.state == .connecting)
        .opacity(session.isLive && session.state != .connecting ? 1 : 0.4)
        .accessibilityLabel(session.isMuted ? "Unmute microphone" : "Mute microphone")
        .help(session.isMuted ? "Unmute microphone" : "Mute microphone")
    }

    private var stopButton: some View {
        Button(action: { Task { await session.end() } }) {
            Image(systemName: "stop.fill")
                .font(.system(size: 20))
                .foregroundStyle(theme.danger)
                .frame(width: sideSize, height: sideSize)
                .overlay(
                    Circle()
                        .strokeBorder(theme.dangerBorder, lineWidth: 0.5)
                )
        }
        .buttonStyle(StopButtonStyle())
        .disabled(!session.isLive)
        .opacity(session.isLive ? 1 : 0.4)
        .accessibilityLabel("End conversation")
        .help("End conversation")
    }
}

private struct HoverButtonStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .opacity(configuration.isPressed ? 0.8 : 1)
    }
}

private struct StopButtonStyle: ButtonStyle {
    @Environment(\.theme) private var theme

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .background(
                Circle()
                    .fill(configuration.isPressed ? theme.dangerTint : Color.clear)
            )
    }
}
