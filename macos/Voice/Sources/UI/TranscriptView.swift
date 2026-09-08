import SwiftUI

struct TranscriptView: View {
    @ObservedObject var session: SessionController
    @Binding var showJumpToLatest: Bool
    var onJumpToLatest: () -> Void

    @Environment(\.theme) private var theme
    private let userInitial = "Y"
    private let agentInitial = "V"

    var body: some View {
        ZStack(alignment: .bottom) {
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 12) {
                        if session.turns.isEmpty && session.interim == nil && !session.isLive {
                            emptyState
                        }
                        ForEach(session.turns) { turn in
                            TurnRow(
                                speaker: turn.speaker,
                                text: session.displayedText(for: turn),
                                userInitial: userInitial,
                                agentInitial: agentInitial
                            )
                            .id(turn.id)
                        }
                        if let interim = session.interim, !session.interimHasExistingRow {
                            InterimRow(text: interim, userInitial: userInitial)
                                .id("interim")
                        }
                        Color.clear.frame(height: 1).id("bottom")
                    }
                    .padding(.horizontal, 16)
                    .padding(.vertical, 14)
                }
                .scrollIndicators(.hidden)
                .onChange(of: session.turns.count) { _, _ in scrollIfNeeded(proxy, animated: true) }
                .onChange(of: session.interim) { _, _ in scrollIfNeeded(proxy, animated: false) }
                .onChange(of: session.turns.last?.text) { _, _ in scrollIfNeeded(proxy, animated: false) }
            }

            if showJumpToLatest {
                Button("Jump to latest", action: onJumpToLatest)
                    .font(.system(size: 11))
                    .foregroundStyle(theme.onTint)
                    .padding(.horizontal, 10)
                    .padding(.vertical, 5)
                    .background(theme.accentTint)
                    .clipShape(Capsule())
                    .padding(.bottom, 12)
                    .buttonStyle(PressableButtonStyle())
            }
        }
        .accessibilityLabel("Conversation transcript")
    }

    private var emptyState: some View {
        Text("Press the orb and start talking")
            .font(.system(size: 13))
            .foregroundStyle(theme.text3)
            .frame(maxWidth: .infinity)
            .padding(.top, 40)
    }

    private func scrollIfNeeded(_ proxy: ScrollViewProxy, animated: Bool) {
        guard !showJumpToLatest else { return }
        if animated {
            withAnimation(.easeOut(duration: 0.12)) {
                proxy.scrollTo("bottom", anchor: .bottom)
            }
        } else {
            var transaction = Transaction()
            transaction.animation = nil
            withTransaction(transaction) {
                proxy.scrollTo("bottom", anchor: .bottom)
            }
        }
    }
}
