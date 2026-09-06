---
name: mac-verify
description: Verify chatbot web sidecar and macOS Voice app, plus computer use (click, type, keys, scroll, drag, screenshot) via sidecar desktop-harness. Use when testing web_app server, Chrome bridge, Voice.app UI, or driving macOS apps.
---

# Mac Verify

Test the web sidecar and native panel without eyeballing.

Project root: `/Users/jack/Documents/chatbot` (run all scripts from there).
Ports: sidecar `http://127.0.0.1:7860/api`, voice `ws://127.0.0.1:8766/v1/realtime`.

## Web sidecar

```bash
# 1. Start services (backend :8766 + sidecar :7860)
./run-browser.sh
# or reuse running: ./run-browser.sh --reuse-running

# 2. Automated check (no browser needed)
uv run pytest tests/test_web_app_server.py -q

# 3. Smoke the API
curl -s http://127.0.0.1:7860/api/health || curl -s http://127.0.0.1:7860/docs
```

Browser verify (needs `browser-tools` skill + Chrome `:9222`):

```bash
# in browser-tools dir:
./browser-start.js --profile
./browser-nav.js http://127.0.0.1:7860/docs
./browser-screenshot.js
./browser-eval.js 'document.title'
```

Use DOM over screenshots: `browser-eval.js 'document.body.innerHTML.slice(0,5000)'`.

See `references/web.md` for endpoint list.

## macOS Voice app

```bash
# 1. Build
./macos/Voice/scripts/build.sh
open macos/Voice/build/Voice.app

# 2. Screenshot check
./.pi/skills/mac-verify/scripts/screenshot.sh Voice
# returns /tmp/voice-*.png -> attach with @/tmp/voice-*.png "verify layout"

# 3. Focus / activate for manual test
osascript -e 'tell application "Voice" to activate'
```

See `references/mac.md` for window/privacy notes.

## Computer use (click / type / keys)

Needs sidecar running + Screen Recording + Accessibility granted (see mac notes).

```bash
./.pi/skills/mac-verify/scripts/act.sh screenshot Voice
./.pi/skills/mac-verify/scripts/act.sh click "Sign in" Voice
./.pi/skills/mac-verify/scripts/act.sh type "hello" Voice
./.pi/skills/mac-verify/scripts/act.sh key return
./.pi/skills/mac-verify/scripts/act.sh hotkey "cmd s"
./.pi/skills/mac-verify/scripts/act.sh scroll 5 Voice
./.pi/skills/mac-verify/scripts/act.sh drag 100 200 300 400
```

Same as Voice's `control_screen` tool, but for pi/`bash`: `screenshot` returns JSON with base64 `image` + `path`. Always `screenshot` first, then act, then `screenshot` again to verify. Never click/type on sign-in or payment windows (sidecar returns 451).

## Efficiency

- Prefer `pytest` + `curl` + `browser-eval.js` over screenshots.
- One screenshot at end to confirm layout, attach with `@path`.
- If sidecar port in use: `lsof -ti TCP:7860 -sTCP:LISTEN | xargs kill` then relaunch.
