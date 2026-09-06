import SwiftUI
import CoreGraphics

struct SettingsView: View {
    @ObservedObject var session: SessionController
    var onClose: () -> Void

    @Environment(\.theme) private var theme
    @State private var webSearch: Bool = VoiceToolExecutor.shared.webSearchEnabled
    @State private var desktopControl: Bool = VoiceToolExecutor.shared.desktopControlEnabled
    @State private var chromeBridge: Bool = VoiceToolExecutor.shared.chromeBridgeEnabled
    @State private var codeAgent: Bool = VoiceToolExecutor.shared.codeAgentEnabled
    @State private var chromeBridgeConnected: Bool = false
    @State private var timer: Timer?
    @State private var memoryText: String = ""
    @State private var memoryNote: String = ""
    @State private var screenCaptureAllowed = CGPreflightScreenCaptureAccess()
    @State private var requestedScreenCapture = false
    @AppStorage("voice.audioMode") private var audioMode = "automatic"

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            // Header
            HStack {
                HStack(spacing: 6) {
                    Image(systemName: "gearshape.fill")
                        .font(.system(size: 14))
                        .foregroundStyle(theme.accent)
                    Text("Tools & Settings")
                        .font(.system(size: 14, weight: .semibold))
                        .foregroundStyle(theme.text)
                }

                Spacer()

                Button(action: onClose) {
                    Image(systemName: "xmark")
                        .font(.system(size: 14))
                        .foregroundStyle(theme.text3)
                        .frame(width: 24, height: 24)
                }
                .buttonStyle(.plain)
                .accessibilityLabel("Close Settings")
            }
            .padding(.bottom, 2)

            Divider()
                .background(theme.border)

            // Tool Toggles
            ScrollView(.vertical, showsIndicators: false) {
                VStack(spacing: 14) {
                    VStack(alignment: .leading, spacing: 6) {
                        Picker("Conversation audio", selection: $audioMode) {
                            Text("Speakers (echo cancellation)").tag("automatic")
                            Text("Headphones (open microphone)").tag("headphones")
                            Text("Speaker compatibility").tag("compatibility")
                        }
                        Text("Applies next conversation. Headphones keeps the microphone open; use it only with headphones. Compatibility mode uses Stop reply instead of spoken interruption.")
                            .font(.system(size: 11)).foregroundStyle(theme.text3)
                    }
                    toolRow(
                        icon: "desktopcomputer",
                        title: "Desktop Control",
                        desc: "Control macOS apps, click buttons, type, press keys, and take screenshots via desktop-harness.",
                        isOn: $desktopControl
                    ) { val in
                        VoiceToolExecutor.shared.desktopControlEnabled = val
                    }

                    VStack(alignment: .leading, spacing: 6) {
                        Text(screenCaptureAllowed ? "Screen Recording allowed" : "Screen Recording needed for screenshots")
                            .font(.system(size: 11)).foregroundStyle(theme.text3)
                        Button("Screen Recording Permission") {
                            if !screenCaptureAllowed && !requestedScreenCapture {
                                requestedScreenCapture = true
                                _ = CGRequestScreenCaptureAccess()
                            }
                            if let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture") {
                                NSWorkspace.shared.open(url)
                            }
                        }
                        Text("Enable Voice in macOS, then quit and reopen Voice. Rebuilding a locally signed app may require permission again.")
                            .font(.system(size: 10.5)).foregroundStyle(theme.text3)
                    }
                    .padding(.leading, 32)

                    toolRow(
                        icon: "magnifyingglass",
                        title: "Web Search & Fetch",
                        desc: "Search Google/TinyFish/Tavily for current facts, news, and fetch public web articles.",
                        isOn: $webSearch
                    ) { val in
                        VoiceToolExecutor.shared.webSearchEnabled = val
                    }

                    VStack(alignment: .leading, spacing: 6) {
                        toolRow(
                            icon: "globe",
                            title: "Chrome Page Bridge",
                            desc: "Read the active webpage, documentation, or X article currently open in Google Chrome.",
                            isOn: $chromeBridge
                        ) { val in
                            VoiceToolExecutor.shared.chromeBridgeEnabled = val
                        }

                        HStack(spacing: 6) {
                            Circle()
                                .fill(chromeBridgeConnected ? theme.accent : theme.text3)
                                .frame(width: 7, height: 7)
                            Text(chromeBridgeConnected ? "Chrome Extension Connected" : "Extension Inactive (load web_app/chrome_article_bridge)")
                                .font(.system(size: 10.5))
                                .foregroundStyle(chromeBridgeConnected ? theme.accent : theme.text3)
                        }
                        .padding(.leading, 32)
                    }

                    toolRow(
                        icon: "chevron.left.forwardslash.chevron.right",
                        title: "Coding Agent",
                        desc: "Let the voice assistant delegate local file edits and coding tasks to an autonomous agent.",
                        isOn: $codeAgent
                    ) { val in
                        VoiceToolExecutor.shared.codeAgentEnabled = val
                    }

                    VStack(alignment: .leading, spacing: 6) {
                        HStack(spacing: 10) {
                            Image(systemName: "brain.head.profile")
                                .font(.system(size: 15))
                                .foregroundStyle(theme.accent)
                                .frame(width: 22, height: 22)
                                .padding(.top, 2)

                            VStack(alignment: .leading, spacing: 2) {
                                Text("Personal Memory")
                                    .font(.system(size: 12.5, weight: .semibold))
                                    .foregroundStyle(theme.text)
                                Text("Facts the assistant remembers across conversations. Say \"remember…\" or edit here.")
                                    .font(.system(size: 11))
                                    .foregroundStyle(theme.text3)
                                    .fixedSize(horizontal: false, vertical: true)
                            }

                            Spacer(minLength: 8)
                        }

                        TextEditor(text: $memoryText)
                            .font(.system(size: 11.5))
                            .foregroundStyle(theme.text)
                            .scrollContentBackground(.hidden)
                            .padding(6)
                            .frame(height: 84)
                            .background(theme.surface2)
                            .clipShape(RoundedRectangle(cornerRadius: Theme.radiusSmall))

                        HStack(spacing: 8) {
                            Text(memoryNote.isEmpty ? "\(memoryText.count) characters" : memoryNote)
                                .font(.system(size: 10.5))
                                .foregroundStyle(theme.text3)
                            Spacer(minLength: 8)
                            Button(action: { Task { await saveMemory() } }) {
                                Text("Save")
                                    .font(.system(size: 12, weight: .medium))
                                    .foregroundStyle(theme.accent)
                                    .padding(.horizontal, 12)
                                    .frame(height: 26)
                                    .background(theme.accentTint)
                                    .clipShape(RoundedRectangle(cornerRadius: Theme.radiusSmall))
                            }
                            .buttonStyle(.plain)
                            .accessibilityLabel("Save personal memory")
                        }
                    }
                    .padding(.vertical, 4)
                }
                .padding(.vertical, 4)
            }

            Spacer(minLength: 0)

            // Footer note
            Text("Changes apply when you start your next conversation.")
                .font(.system(size: 10.5))
                .foregroundStyle(theme.text3)
                .frame(maxWidth: .infinity, alignment: .center)
        }
        .padding(16)
        .background(theme.bg.opacity(0.98))
        .clipShape(RoundedRectangle(cornerRadius: Theme.radius))
        .overlay(
            RoundedRectangle(cornerRadius: Theme.radius)
                .strokeBorder(theme.border, lineWidth: 0.5)
        )
        .onAppear {
            screenCaptureAllowed = CGPreflightScreenCaptureAccess()
            memoryText = session.personalMemory
            checkStatus()
            timer = Timer.scheduledTimer(withTimeInterval: 3.0, repeats: true) { _ in
                checkStatus()
            }
        }
        .onDisappear {
            timer?.invalidate()
            timer = nil
        }
    }

    private func saveMemory() async {
        memoryNote = "Saving…"
        let ok = await session.saveMemory(memoryText)
        await MainActor.run {
            if ok {
                memoryText = session.personalMemory
                memoryNote = "Saved \(session.personalMemory.count) characters"
            } else {
                memoryNote = "Too long — make the profile more compact."
            }
        }
    }

    private func checkStatus() {
        Task {
            let connected = await VoiceToolExecutor.shared.checkChromeBridgeStatus()
            await MainActor.run {
                chromeBridgeConnected = connected
            }
        }
    }

    private func toolRow(
        icon: String,
        title: String,
        desc: String,
        isOn: Binding<Bool>,
        onChange: @escaping (Bool) -> Void
    ) -> some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: icon)
                .font(.system(size: 15))
                .foregroundStyle(theme.accent)
                .frame(width: 22, height: 22)
                .padding(.top, 2)

            VStack(alignment: .leading, spacing: 2) {
                Text(title)
                    .font(.system(size: 12.5, weight: .semibold))
                    .foregroundStyle(theme.text)
                Text(desc)
                    .font(.system(size: 11))
                    .foregroundStyle(theme.text3)
                    .fixedSize(horizontal: false, vertical: true)
            }

            Spacer(minLength: 8)

            Toggle("", isOn: isOn)
                .toggleStyle(.switch)
                .labelsHidden()
                .controlSize(.small)
                .onChange(of: isOn.wrappedValue) { _, newValue in
                    onChange(newValue)
                }
        }
        .padding(.vertical, 4)
    }
}
