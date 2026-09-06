#!/usr/bin/env bash
# Screenshot a macOS app window for pi vision verify.
# Usage: screenshot.sh [AppName] [output]
set -euo pipefail
APP="${1:-Voice}"
OUT="${2:-/tmp/voice-$(date +%H%M%S).png}"
osascript -e "tell application \"$APP\" to activate" 2>/dev/null || true
sleep 0.8
screencapture -x "$OUT"
echo "$OUT"
