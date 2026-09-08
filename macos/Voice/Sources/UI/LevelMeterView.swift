import SwiftUI

struct LevelMeterView: View {
    let state: SessionState
    let isMuted: Bool
    @ObservedObject var levels: AudioLevels

    @Environment(\.theme) private var theme
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private let barCount = 20
    private let barWidth: CGFloat = 2
    private let barGap: CGFloat = 3
    private let minHeight: CGFloat = 4
    private let maxHeight: CGFloat = 20

    var body: some View {
        Group {
            if isMuted && isLive {
                Text("Muted")
                    .font(.system(size: 11))
                    .foregroundStyle(theme.onTint)
                    .padding(.horizontal, 8)
                    .padding(.vertical, 3)
                    .background(theme.accentTint)
                    .clipShape(RoundedRectangle(cornerRadius: Theme.radiusSmall))
            } else {
                HStack(spacing: barGap) {
                    ForEach(0..<barCount, id: \.self) { index in
                        RoundedRectangle(cornerRadius: 2)
                            .fill(barColor)
                            .frame(width: barWidth, height: barHeight(at: index))
                    }
                }
            }
        }
        .frame(height: 28)
        .frame(maxWidth: .infinity)
        .accessibilityHidden(true)
    }

    private var isLive: Bool {
        switch state {
        case .idle, .failed: return false
        default: return true
        }
    }

    private var activeLevel: Float {
        switch state {
        case .agentSpeaking: return levels.output
        case .listening, .connecting: return levels.input
        default: return 0
        }
    }

    private var barColor: Color {
        switch state {
        case .agentSpeaking: return theme.accentDeep
        case .listening, .connecting: return theme.accent
        default: return theme.border
        }
    }

    private func barHeight(at index: Int) -> CGFloat {
        guard isLive else { return minHeight }
        let center = Double(barCount - 1) / 2
        let dist = abs(Double(index) - center) / center
        let wave = 0.35 + 0.65 * (1 - dist)
        let level = reduceMotion ? 0.5 : Double(activeLevel)
        return minHeight + CGFloat(level) * CGFloat(wave) * (maxHeight - minHeight)
    }
}
