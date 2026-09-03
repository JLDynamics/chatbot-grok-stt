# Native macOS Voice panel

This replaces the browser UI (`web_app/`) as the product surface for chatting
with the Chatbot voice backend. Spec comes from `~/voice-chat-window-design`.

## Build

```bash
./macos/Voice/scripts/build.sh
open macos/Voice/build/Voice.app
```

Requires the voice server on `ws://127.0.0.1:8766/v1/realtime` (for example
`./run-browser.sh` or `./run-openrouter.sh`).

## Backends

- Default: `LiveVoiceBackend` (real mic + WebSocket).
- Mock UI-only: `defaults write com.jack.Voice voice.useMock -bool true`
- Custom URL: `defaults write com.jack.Voice voice.wsUrl -string 'ws://…'`

## Layout

```
macos/Voice/
  Sources/App/       FloatingPanel, PanelController, VoiceApp, GlobalHotKey
  Sources/UI/        Theme + studio panel views
  Sources/Session/   VoiceBackend, SessionController, Mock + Live backends
  Sources/Audio/     AudioEngine (shared mic/playback, AEC when available)
  Resources/         Info.plist, entitlements
  scripts/build.sh
```
