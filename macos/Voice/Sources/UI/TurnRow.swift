import SwiftUI

struct TurnRow: View {
    let turn: Turn
    let userInitial: String
    let agentInitial: String

    @Environment(\.theme) private var theme
    @State private var hovering = false

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            avatar
            Text(turn.text)
                .font(.system(size: 14))
                .lineSpacing(14 * 0.55)
                .foregroundStyle(turn.speaker == .agent ? theme.text : theme.text2)
                .frame(maxWidth: .infinity, alignment: .leading)
            if hovering {
                Text(turn.at, style: .time)
                    .font(.system(size: 11))
                    .foregroundStyle(theme.text3)
            }
        }
        .onHover { hovering = $0 }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(turn.speaker == .agent ? "Assistant" : "You"): \(turn.text)")
    }

    private var avatar: some View {
        let initial = turn.speaker == .agent ? agentInitial : userInitial
        let bg = turn.speaker == .agent ? theme.accentTint : theme.surface2
        let fg = turn.speaker == .agent ? theme.onTint : theme.text2
        return Text(initial)
            .font(.system(size: 11, weight: .medium))
            .foregroundStyle(fg)
            .frame(width: 24, height: 24)
            .background(bg)
            .clipShape(Circle())
    }
}

struct InterimRow: View {
    let text: String
    let userInitial: String

    @Environment(\.theme) private var theme

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            Text(userInitial)
                .font(.system(size: 11, weight: .medium))
                .foregroundStyle(theme.text2)
                .frame(width: 24, height: 24)
                .background(theme.surface2)
                .clipShape(Circle())
            Text(text)
                .font(.system(size: 14))
                .italic()
                .lineSpacing(14 * 0.55)
                .foregroundStyle(theme.text3)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
        .accessibilityHidden(true)
    }
}
