import SwiftUI

struct TranscriptView: View {
    @ObservedObject var session: SessionController
    @Binding var showJumpToLatest: Bool
    @Binding var stickToBottom: Bool
    var onJumpToLatest: () -> Void

    @Environment(\.theme) private var theme
    private let userInitial = "Y"
    private let agentInitial = "V"

    var body: some View {
        ScrollViewReader { proxy in
            ZStack(alignment: .bottom) {
                ScrollView {
                    // A regular stack, not LazyVStack: token-by-token height
                    // changes plus lazy recycling were bouncing the scroller
                    // up and then snapping it back to the bottom.
                    VStack(alignment: .leading, spacing: 12) {
                        if session.turns.isEmpty && session.interim == nil && !session.userSpeaking && !session.isLive {
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
                        // A live caption only exists when the server is running
                        // progressive STT. Without it, show that the user is
                        // talking rather than an unstable guess at the words.
                        if let interim = session.interim, !session.interimHasExistingRow {
                            InterimRow(text: interim, userInitial: userInitial)
                                .id("interim")
                        } else if session.userSpeaking {
                            SpeakingRow(levels: session.levels, userInitial: userInitial)
                                .id("speaking")
                        }
                        Color.clear.frame(height: 1).id("bottom")
                    }
                    .padding(.horizontal, 16)
                    .padding(.vertical, 14)
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
                .defaultScrollAnchor(.bottom)
                .scrollIndicators(.never)
                .onChange(of: session.turns.count) { _, _ in
                    pinToBottomIfNeeded(proxy)
                }
                .onChange(of: session.userSpeaking) { _, _ in
                    pinToBottomIfNeeded(proxy)
                }
                .onChange(of: stickToBottom) { _, pinned in
                    if pinned { pinToBottomIfNeeded(proxy) }
                }
                .modifier(TranscriptScrollPin(awayFromBottom: $showJumpToLatest, stickToBottom: $stickToBottom))

                if showJumpToLatest {
                    Button("Jump to latest") {
                        onJumpToLatest()
                        pinToBottomIfNeeded(proxy)
                    }
                    .font(.system(size: 11))
                    .foregroundStyle(theme.onTint)
                    .padding(.horizontal, 10)
                    .padding(.vertical, 5)
                    .background(theme.accentTint)
                    .clipShape(Capsule())
                    .padding(.bottom, 12)
                    .buttonStyle(PressableButtonStyle())
                    .accessibilityIdentifier("voice.jumpLatest")
                }
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

    private func pinToBottomIfNeeded(_ proxy: ScrollViewProxy) {
        guard stickToBottom else { return }
        var transaction = Transaction()
        transaction.animation = nil
        withTransaction(transaction) {
            proxy.scrollTo("bottom", anchor: .bottom)
        }
    }
}

/// Keeps the jump-to-latest chip in sync with the user's scroll position
/// without calling `scrollTo` on every streamed token.
private struct TranscriptScrollPin: ViewModifier {
    @Binding var awayFromBottom: Bool
    @Binding var stickToBottom: Bool

    func body(content: Content) -> some View {
        if #available(macOS 15.0, *) {
            content.onScrollGeometryChange(for: Bool.self) { geo in
                geo.contentSize.height - geo.contentOffset.y - geo.containerSize.height > 48
            } action: { _, away in
                if away != awayFromBottom {
                    awayFromBottom = away
                    stickToBottom = !away
                }
            }
        } else {
            content
        }
    }
}
