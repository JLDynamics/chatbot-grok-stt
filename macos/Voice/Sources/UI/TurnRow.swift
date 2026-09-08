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
