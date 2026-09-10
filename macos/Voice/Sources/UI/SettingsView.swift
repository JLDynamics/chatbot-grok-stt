import SwiftUI
import CoreGraphics

struct SettingsView: View {
    @ObservedObject var session: SessionController
    var onClose: () -> Void

    @Environment(\.theme) private var theme
    @State private var webSearch: Bool = VoiceToolExecutor.shared.webSearchEnabled
    @State private var screenshot: Bool = VoiceToolExecutor.shared.screenshotEnabled
    @State private var chromeBridge: Bool = VoiceToolExecutor.shared.chromeBridgeEnabled
    @State private var codeAgent: Bool = VoiceToolExecutor.shared.codeAgentEnabled
    @State private var chromeBridgeConnected: Bool = false
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
                .buttonStyle(PressableButtonStyle())
                .contentShape(Rectangle())
                .accessibilityLabel("Close Settings")
                .accessibilityIdentifier("voice.settings.close")
            }
            .padding(.bottom, 2)

            Divider()
                .background(theme.border)

            // Tool Toggles
            ScrollView(.vertical, showsIndicators: true) {
                VStack(spacing: 14) {
                    sidecarStatusRow

                    personalMemorySection

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
                        icon: "camera.viewfinder",
                        title: "Screenshot",
                        desc: "Capture what is on screen so the assistant can see a layout, image, or chart. Articles use bash + curl.",
                        isOn: $screenshot
                    ) { val in
                        VoiceToolExecutor.shared.screenshotEnabled = val
                        session.applyToolSettings()
                    }

                    VStack(alignment: .leading, spacing: 6) {
                        Text(screenCaptureAllowed ? "Screen Recording allowed for this Voice build" : "Screen Recording needed for screenshots")
                            .font(.system(size: 11)).foregroundStyle(theme.text3)
                        Button("Screen Recording Permission") {
                            requestedScreenCapture = true
                            _ = ScreenCapture.requestAccess()
                            screenCaptureAllowed = ScreenCapture.isAllowed
                            if let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture") {
                                NSWorkspace.shared.open(url)
                            }
                        }
                        Text(screenCaptureAllowed
                             ? "This running Voice.app can capture the screen."
                             : "The list can show Voice as on from an older copy. Click Screen Recording Permission so THIS build can prompt. If it is already on: remove Voice, add the Voice.app you launched, then quit and reopen.")
                            .font(.system(size: 10.5)).foregroundStyle(theme.text3)
                    }
                    .padding(.leading, 32)

                    toolRow(
                        icon: "magnifyingglass",
                        title: "Web research (bash + curl)",
                        desc: "The voice model checks current facts itself with curl, inside the same reply. Latest news should use a dated RSS feed. The coding agent is not used for web research.",
                        isOn: $webSearch
                    ) { val in
                        VoiceToolExecutor.shared.webSearchEnabled = val
                        session.applyToolSettings()
                    }
                    availabilityNote("This branch uses curl instead of TinyFish/Tavily. Search API keys are unused.")

                    VStack(alignment: .leading, spacing: 6) {
                        toolRow(
                            icon: "globe",
                            title: "Chrome Page Bridge",
                            desc: "Read the active webpage, documentation, or X article currently open in Google Chrome.",
                            isOn: $chromeBridge
                        ) { val in
                            VoiceToolExecutor.shared.chromeBridgeEnabled = val
                            session.applyToolSettings()
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
                        desc: "Hands a coding or file task to Grok on this Mac (read/edit files, run commands). Not used for news, search, or reading websites.",
                        isOn: $codeAgent
                    ) { val in
                        VoiceToolExecutor.shared.codeAgentEnabled = val
                        session.applyToolSettings()
                    }
                    if session.sidecarConfig?.codeAgent == false {
                        availabilityNote("Coding agent is turned off on the server (CODE_AGENT=off).")
                    }
                }
                .padding(.vertical, 4)
            }

            Spacer(minLength: 0)

            // Footer note
            Text("Tool toggles apply on the next reply. Audio mode applies when you start the next conversation.")
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
        .task {
            screenCaptureAllowed = CGPreflightScreenCaptureAccess()
            if memoryText.isEmpty { memoryText = session.personalMemory }
            memoryNote = session.personalMemory.isEmpty ? "Loading…" : memoryNote
            await session.loadMemory()
            await session.refreshConfig()
            memoryText = session.personalMemory
            if let error = session.memoryError {
                memoryNote = error
            } else {
                memoryNote = session.personalMemory.isEmpty ? "Nothing saved yet." : ""
            }
            await pollStatus()
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 8_000_000_000)
                await pollStatus()
            }
        }
        .onAppear {
            screenCaptureAllowed = CGPreflightScreenCaptureAccess()
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
                memoryNote = session.memoryError ?? "Could not save personal memory."
            }
        }
    }

    private var sidecarStatusRow: some View {
        HStack(spacing: 6) {
            Circle()
                .fill(session.sidecarConfig != nil ? theme.accent : theme.text3)
                .frame(width: 7, height: 7)
            Text(session.sidecarConfig != nil
                 ? "Local data connected (memory and conversations)"
                 : (session.memoryError ?? "Connecting to local data…"))
                .font(.system(size: 10.5))
                .foregroundStyle(session.sidecarConfig != nil ? theme.accent : theme.text3)
        }
    }

    private var personalMemorySection: some View {
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
                .accessibilityLabel("Personal memory")

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
                .buttonStyle(PressableButtonStyle())
                .accessibilityLabel("Save personal memory")
            }
        }
        .padding(.vertical, 4)
        .onChange(of: session.personalMemory) { oldValue, newValue in
            if memoryText == oldValue || memoryText.isEmpty {
                memoryText = newValue
            }
        }
    }

    private func availabilityNote(_ text: String) -> some View {
        Text(text)
            .font(.system(size: 10.5))
            .foregroundStyle(theme.text3)
            .padding(.leading, 32)
            .padding(.top, -8)
    }

    private func pollStatus() async {
        let connected = await VoiceToolExecutor.shared.checkChromeBridgeStatus()
        chromeBridgeConnected = connected
        screenCaptureAllowed = ScreenCapture.isAllowed
    }

    private func toolIdentifier(_ title: String) -> String {
        switch title {
        case "Screenshot": return "voice.tools.screenshot"
        case "Web research (bash + curl)": return "voice.tools.search"
        case "Chrome Page Bridge": return "voice.tools.chrome"
        default: return "voice.tools.codeAgent"
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
                .accessibilityLabel(title)
                .accessibilityIdentifier(toolIdentifier(title))
                .onChange(of: isOn.wrappedValue) { _, newValue in
                    onChange(newValue)
                }
        }
        .padding(.vertical, 4)
    }
}
