import SwiftUI

struct TurnRow: View {
    let speaker: Turn.Speaker
    let text: String
    let userInitial: String
    let agentInitial: String

    @Environment(\.theme) private var theme

    private var isAgent: Bool { speaker == .agent }

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            avatar
            VStack(alignment: .leading, spacing: 3) {
                Text(isAgent ? "Voice" : "You")
                    .font(.system(size: 10.5, weight: .semibold))
                    .foregroundStyle(isAgent ? theme.accent : theme.text3)
                Text(text)
                    .font(.system(size: 14))
                    .lineSpacing(4)
                    .foregroundStyle(isAgent ? theme.text : theme.text2)
                    .fixedSize(horizontal: false, vertical: true)
                    .textSelection(.enabled)
            }
            .padding(.horizontal, isAgent ? 0 : 8)
            .padding(.vertical, isAgent ? 0 : 6)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(isAgent ? Color.clear : theme.surface2)
            .clipShape(RoundedRectangle(cornerRadius: 8))
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(isAgent ? "Assistant" : "You"): \(text)")
    }

    private var avatar: some View {
        let initial = isAgent ? agentInitial : userInitial
        let bg = isAgent ? theme.accentTint : theme.surface2
        let fg = isAgent ? theme.onTint : theme.text2
        return Text(initial)
            .font(.system(size: 11, weight: .semibold))
            .foregroundStyle(fg)
            .frame(width: 24, height: 24)
            .background(bg)
            .clipShape(Circle())
            .overlay(
                Circle().strokeBorder(isAgent ? theme.accent.opacity(0.35) : theme.border, lineWidth: 0.5)
            )
            .accessibilityHidden(true)
    }
}

struct InterimRow: View {
    let text: String
    let userInitial: String

    @Environment(\.theme) private var theme

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            Text(userInitial)
                .font(.system(size: 11, weight: .semibold))
                .foregroundStyle(theme.text2)
                .frame(width: 24, height: 24)
                .background(theme.surface2)
                .clipShape(Circle())
                .overlay(Circle().strokeBorder(theme.border, lineWidth: 0.5))
            VStack(alignment: .leading, spacing: 3) {
                Text("You")
                    .font(.system(size: 10.5, weight: .semibold))
                    .foregroundStyle(theme.text3)
                Text(text)
                    .font(.system(size: 14))
                    .italic()
                    .lineSpacing(4)
                    .foregroundStyle(theme.text3)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .padding(.horizontal, 8)
            .padding(.vertical, 6)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(theme.surface2.opacity(0.7))
            .clipShape(RoundedRectangle(cornerRadius: 8))
        }
        .accessibilityHidden(true)
    }
}

/// Stands in for a live transcript while the user is talking.
///
/// Streaming ASR necessarily revises itself — a re-decode of the same audio
/// changes words — so showing the words as they arrive can only ever look
/// unstable. The bars follow the real mic level instead, which answers the
/// question the caption was actually being read for: is it hearing me?
struct SpeakingRow: View {
    @ObservedObject var levels: AudioLevels
    let userInitial: String

    @Environment(\.theme) private var theme

    /// Fixed per-bar response, so the row reads as one voice envelope rather
    /// than five copies of the same bar.
    private let response: [CGFloat] = [0.45, 0.75, 1.0, 0.7, 0.4]
    private let minHeight: CGFloat = 3
    private let maxHeight: CGFloat = 16

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            Text(userInitial)
                .font(.system(size: 11, weight: .semibold))
                .foregroundStyle(theme.text2)
                .frame(width: 24, height: 24)
                .background(theme.surface2)
                .clipShape(Circle())
                .overlay(Circle().strokeBorder(theme.border, lineWidth: 0.5))
            VStack(alignment: .leading, spacing: 3) {
                Text("You")
                    .font(.system(size: 10.5, weight: .semibold))
                    .foregroundStyle(theme.text3)
                HStack(alignment: .center, spacing: 3) {
                    ForEach(response.indices, id: \.self) { index in
                        Capsule()
                            .fill(theme.text3)
                            .frame(width: 3, height: barHeight(at: index))
                    }
                }
                .frame(height: maxHeight, alignment: .center)
                // Matches the orb's meter smoothing so the two agree.
                .animation(.linear(duration: 0.09), value: levels.input)
                .accessibilityLabel("Listening")
            }
            .padding(.horizontal, 8)
            .padding(.vertical, 6)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(theme.surface2.opacity(0.7))
            .clipShape(RoundedRectangle(cornerRadius: 8))
        }
    }

    private func barHeight(at index: Int) -> CGFloat {
        let level = CGFloat(min(max(levels.input, 0), 1))
        return minHeight + (maxHeight - minHeight) * level * response[index]
    }
}
