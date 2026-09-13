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
                    transcriptStack
                        .padding(.horizontal, 16)
                        .padding(.vertical, 14)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                .defaultScrollAnchor(.bottom)
                .modifier(BottomAnchoredThroughSizeChanges())
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

    /// Only the rows on screen are built and measured.
    ///
    /// This used to be a plain VStack, because lazy recycling plus token-by-token
    /// height changes bounced the scroller up and then snapped it back. A plain
    /// stack measures *every* turn on every layout pass, so a long conversation
    /// made resizing the window progressively slower — the cost grew with the
    /// transcript. The bounce is handled directly now, by the bottom anchor in
    /// `BottomAnchoredThroughSizeChanges`, so the stack can be lazy again.
    @ViewBuilder
    private var transcriptStack: some View {
        if #available(macOS 15.0, *) {
            LazyVStack(alignment: .leading, spacing: 12) { rows }
        } else {
            VStack(alignment: .leading, spacing: 12) { rows }
        }
    }

    @ViewBuilder
    private var rows: some View {
        if session.turns.isEmpty && !session.userSpeaking && !session.isLive {
            emptyState
        }
        ForEach(session.turns) { turn in
            TurnRow(
                speaker: turn.speaker,
                text: turn.text,
                userInitial: userInitial,
                agentInitial: agentInitial
            )
            .id(turn.id)
        }
        if session.userSpeaking {
            SpeakingRow(levels: session.levels, userInitial: userInitial)
                .id("speaking")
        }
        Color.clear.frame(height: 1).id("bottom")
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


/// Holds the transcript against the bottom of the scroll view while the content
/// size changes underneath it — a streamed token growing a row, or a lazy row
/// being measured for the first time. Without this, a lazy stack slides.
private struct BottomAnchoredThroughSizeChanges: ViewModifier {
    @ViewBuilder
    func body(content: Content) -> some View {
        if #available(macOS 15.0, *) {
            content.defaultScrollAnchor(.bottom, for: .sizeChanges)
        } else {
            content
        }
    }
}
