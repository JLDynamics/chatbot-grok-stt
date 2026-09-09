# Native macOS Voice panel

This replaces the browser UI (`web_app/`) as the product surface for chatting
with the Chatbot voice backend. Spec comes from `~/voice-chat-window-design`.

## Build and run

Install into `/Applications` (Finder, Launchpad, Spotlight) and open that copy:

```bash
./macos/Voice/scripts/install.sh
open /Applications/Voice.app
```

`install.sh` builds first unless you pass `--skip-build`. The compiler writes
`macos/Voice/build/Voice.app`; that folder is not meant to appear in Launchpad.
Always launch **Applications → Voice**.

Requires the voice server on `ws://127.0.0.1:8766/v1/realtime`. Clicking the
app starts `./run-browser.sh` when those default local ports are used, and
restarts a backend whose source fingerprint no longer matches this checkout.

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
