import SwiftUI

enum ThemePreference: String, CaseIterable, Identifiable {
    case auto, light, dark
    var id: String { rawValue }
    var label: String {
        switch self {
        case .auto: return "Auto"
        case .light: return "Light"
        case .dark: return "Dark"
        }
    }
}

struct HeaderView: View {
    @Binding var alwaysOnTop: Bool
    @Binding var themePreference: ThemePreference
    var errorText: String?
    var onPinToggle: () -> Void
    var onSettingsToggle: () -> Void
    var onHistoryToggle: () -> Void
    var onClose: () -> Void

    @Environment(\.theme) private var theme
    @Environment(\.colorScheme) private var systemScheme

    var body: some View {
        HStack(spacing: 8) {
            Button(action: {
                alwaysOnTop.toggle()
                onPinToggle()
            }) {
                HStack(spacing: 5) {
                    Image(systemName: alwaysOnTop ? "pin.fill" : "pin")
                        .font(.system(size: 16))
                    Text("On top")
                        .font(.system(size: 12))
                }
                .foregroundStyle(alwaysOnTop ? theme.accent : theme.text3)
                .padding(.horizontal, 7)
                .frame(height: 28)
                .background(alwaysOnTop ? theme.accentTint : Color.clear)
                .clipShape(RoundedRectangle(cornerRadius: Theme.radiusSmall))
            }
            .buttonStyle(PressableButtonStyle())
            .contentShape(RoundedRectangle(cornerRadius: Theme.radiusSmall))
            .accessibilityLabel("Always on top")
            .accessibilityIdentifier("voice.onTop")
            .accessibilityAddTraits(alwaysOnTop ? .isSelected : [])

            if let errorText, !errorText.isEmpty {
                Text(errorText)
                    .font(.system(size: 11))
                    .foregroundStyle(theme.danger)
                    .lineLimit(1)
                    .truncationMode(.tail)
                    .help(errorText)
            }

            // Must be the hit-tested view itself (not a .background); SwiftUI
            // Color.clear eats mouseDown before a background NSView sees it.
            WindowDragArea()
                .frame(maxWidth: .infinity, maxHeight: .infinity)

            themePicker
            historyButton
            settingsButton
            closeButton
        }
        .padding(.horizontal, 10)
        .frame(height: 40)
        .background(theme.bg.opacity(0.92))
        .overlay(alignment: .bottom) {
            Rectangle()
                .fill(theme.border)
                .frame(height: 0.5)
        }
    }

    private var themePicker: some View {
        HStack(spacing: 0) {
            ForEach(ThemePreference.allCases) { pref in
                Button(pref.label) {
                    themePreference = pref
                }
                .font(.system(size: 11))
                .foregroundStyle(themePreference == pref ? theme.bg : theme.text3)
                .padding(.horizontal, 8)
                .padding(.vertical, 4)
                .background(themePreference == pref ? theme.text : Color.clear)
                .clipShape(RoundedRectangle(cornerRadius: Theme.radiusSmall))
                .buttonStyle(PressableButtonStyle())
                .accessibilityIdentifier("voice.theme.\(pref.rawValue)")
                .accessibilityAddTraits(themePreference == pref ? .isSelected : [])
            }
        }
        .padding(2)
        .background(theme.surface2)
        .clipShape(RoundedRectangle(cornerRadius: Theme.radiusSmall))
        .frame(height: 22)
        .accessibilityLabel("Theme")
    }

    private var historyButton: some View {
        Button(action: onHistoryToggle) {
            Image(systemName: "clock.arrow.circlepath")
                .font(.system(size: 15))
                .foregroundStyle(theme.text3)
                .frame(width: 28, height: 28)
        }
        .buttonStyle(PressableButtonStyle())
        .contentShape(Rectangle())
        .accessibilityLabel("Conversations")
        .accessibilityIdentifier("voice.history")
        .help("Saved conversations")
    }

    private var settingsButton: some View {
        Button(action: onSettingsToggle) {
            Image(systemName: "gearshape")
                .font(.system(size: 15))
                .foregroundStyle(theme.text3)
                .frame(width: 28, height: 28)
        }
        .buttonStyle(PressableButtonStyle())
        .contentShape(Rectangle())
        .accessibilityLabel("Settings")
        .accessibilityIdentifier("voice.settings")
        .help("Tools & Settings")
    }

    private var closeButton: some View {
        Button(action: onClose) {
            Image(systemName: "xmark")
                .font(.system(size: 16))
                .foregroundStyle(theme.text3)
                .frame(width: 28, height: 28)
        }
        .buttonStyle(PressableButtonStyle())
        .contentShape(Rectangle())
        .accessibilityLabel("Close")
        .accessibilityIdentifier("voice.closePanel")
        .help("Hide panel")
    }
}
