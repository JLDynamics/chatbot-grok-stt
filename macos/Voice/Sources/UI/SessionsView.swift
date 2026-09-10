import SwiftUI

/// Saved-conversation picker, mirroring the web sidebar session list.
/// Same REST store (`ChatStore`), so web and native share history.
struct SessionsView: View {
    @ObservedObject var session: SessionController
    var onClose: () -> Void

    @Environment(\.theme) private var theme
    @State private var working = false

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                HStack(spacing: 6) {
                    Image(systemName: "clock.arrow.circlepath")
                        .font(.system(size: 14))
                        .foregroundStyle(theme.accent)
                    Text("Conversations")
                        .font(.system(size: 14, weight: .semibold))
                        .foregroundStyle(theme.text)
                }

                Spacer()

                Button(action: { Task { await newChat() } }) {
                    HStack(spacing: 4) {
                        Image(systemName: "plus")
                            .font(.system(size: 11, weight: .semibold))
                        Text("New")
                            .font(.system(size: 12, weight: .medium))
                    }
                    .foregroundStyle(theme.accent)
                    .padding(.horizontal, 9)
                    .frame(height: 26)
                    .background(theme.accentTint)
                    .clipShape(RoundedRectangle(cornerRadius: Theme.radiusSmall))
                }
                .buttonStyle(PressableButtonStyle())
                .disabled(working)
                .accessibilityLabel("New conversation")
                .accessibilityIdentifier("voice.history.new")

                Button(action: onClose) {
                    Image(systemName: "xmark")
                        .font(.system(size: 14))
                        .foregroundStyle(theme.text3)
                        .frame(width: 24, height: 24)
                }
                .buttonStyle(PressableButtonStyle())
                .contentShape(Rectangle())
                .accessibilityLabel("Close conversations")
                .accessibilityIdentifier("voice.history.close")
            }

            Divider().background(theme.border)

            if working && session.sessions.isEmpty {
                HStack {
                    Spacer()
                    ProgressView()
                        .controlSize(.small)
                    Spacer()
                }
                .padding(.vertical, 12)
            } else if session.sessions.isEmpty, let error = session.sessionsError {
                Text(error)
                    .font(.system(size: 11.5))
                    .foregroundStyle(theme.text3)
                    .frame(maxWidth: .infinity, alignment: .center)
                    .padding(.vertical, 12)
            } else if session.sessions.isEmpty {
                Text("No saved conversations yet. Start talking and they will appear here.")
                    .font(.system(size: 11.5))
                    .foregroundStyle(theme.text3)
                    .frame(maxWidth: .infinity, alignment: .center)
                    .padding(.vertical, 12)
            } else {
                ScrollView(.vertical, showsIndicators: false) {
                    VStack(spacing: 6) {
                        ForEach(session.sessions) { row in
                            sessionRow(row)
                        }
                    }
                    .padding(.vertical, 2)
                }
                .frame(maxHeight: 260)
            }
        }
        .padding(16)
        .background(theme.bg.opacity(0.98))
        .clipShape(RoundedRectangle(cornerRadius: Theme.radius))
        .overlay(
            RoundedRectangle(cornerRadius: Theme.radius)
                .strokeBorder(theme.border, lineWidth: 0.5)
        )
        .task {
            working = true
            await session.refreshSessions()
            working = false
        }
    }

    private func sessionRow(_ row: ChatSessionSummary) -> some View {
        let isActive = row.id == session.currentSessionId
        return HStack(spacing: 8) {
            Button(action: { Task { await open(row) } }) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(row.title.isEmpty ? "New conversation" : row.title)
                        .font(.system(size: 12.5, weight: .medium))
                        .foregroundStyle(theme.text)
                        .lineLimit(1)
                    if let preview = row.preview, !preview.isEmpty {
                        Text(preview)
                            .font(.system(size: 11))
                            .foregroundStyle(theme.text3)
                            .lineLimit(1)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.horizontal, 10)
                .padding(.vertical, 7)
                .background(isActive ? theme.accentTint : theme.surface2)
                .clipShape(RoundedRectangle(cornerRadius: Theme.radiusSmall))
            }
            .buttonStyle(PressableButtonStyle())
            .disabled(working)
            .accessibilityLabel("Open \(row.title)")

            Button(action: { Task { await session.deleteSession(id: row.id) } }) {
                Image(systemName: "trash")
                    .font(.system(size: 12))
                    .foregroundStyle(theme.danger)
                    .frame(width: 26, height: 26)
            }
            .buttonStyle(PressableButtonStyle())
            .disabled(working)
            .accessibilityLabel("Delete \(row.title)")
            .help("Delete conversation")
        }
    }

    private func open(_ row: ChatSessionSummary) async {
        working = true
        defer { working = false }
        await session.openSession(id: row.id)
        onClose()
    }

    private func newChat() async {
        working = true
        defer { working = false }
        await session.newSession()
        onClose()
    }
}
