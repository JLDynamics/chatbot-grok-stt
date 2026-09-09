import SwiftUI

/// Primary control and status indicator. Diameter is fixed; only the ring animates.
struct OrbView: View {
    let state: SessionState
    let isMuted: Bool
    @ObservedObject var levels: AudioLevels
    let onTap: () -> Void

    @Environment(\.theme) private var theme
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private let diameter: CGFloat = 64

    var body: some View {
        Button(action: onTap) {
            ZStack {
                orbFill
                    .frame(width: diameter, height: diameter)
                    .clipShape(Circle())
                    .opacity(orbOpacity)
                ringOverlay
            }
            .frame(width: diameter, height: diameter)
        }
        .buttonStyle(PressableButtonStyle(pressedScale: 0.96))
        .contentShape(Circle())
        .accessibilityLabel(accessibilityLabel)
        .help(helpText)
    }

    private var orbOpacity: Double {
        if case .failed = state { return 0.55 }
        if case .idle = state { return 0.55 }
        return 1
    }

    private var isLive: Bool {
        switch state {
        case .idle, .failed: return false
        default: return true
        }
    }

    @ViewBuilder
    private var orbFill: some View {
        if isMuted && isLive {
            OrbGradient(stops: theme.orbMutedStops)
        } else {
            OrbGradient(stops: theme.orbStops)
        }
    }

    @ViewBuilder
    private var ringOverlay: some View {
        switch displayMode {
        case .idle:
            EmptyView()
        case .connecting:
            if reduceMotion {
                Circle().strokeBorder(theme.accent, lineWidth: 2)
            } else {
                ConnectingRing(color: theme.accent)
            }
        case .listening:
            Circle()
                .strokeBorder(theme.orbRing, lineWidth: 2)
                .padding(-ringSpread(for: levels.input))
                .animation(.linear(duration: 0.09), value: levels.input)
        case .thinking:
            if reduceMotion {
                Circle().strokeBorder(theme.orbRing, lineWidth: 2).padding(-4)
            } else {
                ConnectingRing(color: theme.orbRing)
            }
        case .agentSpeaking:
            if reduceMotion {
                Circle().strokeBorder(theme.orbRing, lineWidth: 6)
            } else {
                BreathingRing(color: theme.orbRing)
            }
        case .muted:
            Circle().strokeBorder(theme.orbRing, lineWidth: 2)
                .padding(-4)
        case .error:
            Circle().strokeBorder(theme.danger, lineWidth: 2)
        }
    }

    private enum DisplayMode {
        case idle, connecting, listening, thinking, agentSpeaking, muted, error
    }

    private var displayMode: DisplayMode {
        if case .failed = state { return .error }
        if isMuted && isLive { return .muted }
        switch state {
        case .idle: return .idle
        case .connecting: return .connecting
        case .listening: return .listening
        case .thinking: return .thinking
        case .agentSpeaking: return .agentSpeaking
        case .failed: return .error
        }
    }

    private func ringSpread(for level: Float) -> CGFloat {
        4 + CGFloat(level) * 8
    }

    private var accessibilityLabel: String {
        switch state {
        case .idle: return "Start conversation"
        case .failed: return "Retry conversation"
        default: return "End conversation"
        }
    }

    private var helpText: String {
        switch state {
        case .idle: return "Start conversation"
        case .failed: return "Retry conversation"
        default: return "End conversation"
        }
    }
}

private struct ConnectingRing: View {
    let color: Color
    @State private var rotation: Double = 0

    var body: some View {
        Circle()
            .trim(from: 0, to: 0.75)
            .stroke(color, style: StrokeStyle(lineWidth: 2, lineCap: .round))
            .rotationEffect(.degrees(rotation))
            .onAppear {
                withAnimation(.linear(duration: 1.2).repeatForever(autoreverses: false)) {
                    rotation = 360
                }
            }
    }
}

private struct BreathingRing: View {
    let color: Color
    @State private var spread: CGFloat = 6

    var body: some View {
        Circle()
            .strokeBorder(color, lineWidth: 2)
            .padding(-spread)
            .onAppear {
                withAnimation(.easeInOut(duration: 2.4).repeatForever(autoreverses: true)) {
                    spread = 11
                }
            }
    }
}
