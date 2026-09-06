# macOS Voice reference

- Sources: `macos/Voice/Sources/{App,UI,Session,Audio}/` (`VoiceApp.swift:@main`, `SessionController`, `LiveVoiceBackend`).
- Build: `macos/Voice/scripts/build.sh` → `macos/Voice/build/Voice.app` (swiftc, no Xcode project). `test.sh` for tests.
- Signing: persistent `VOICE_SIGNING_IDENTITY` keeps privacy grants; ad-hoc breaks them.
- Verify: `open build/Voice.app`, `osascript -e 'tell application "Voice" to activate'`, `screencapture -x /tmp/x.png`.
- In pi: `pi @/tmp/x.png "verify panel"` — vision check, no GUI driver needed.
- For clicks/keys (no built-in computer-use): add `cliclick` later, e.g. `cliclick c:100,200`.
