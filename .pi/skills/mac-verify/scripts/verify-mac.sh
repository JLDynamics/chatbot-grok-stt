#!/usr/bin/env bash
# macOS Voice.app verify: build + launch + screenshot path.
# Usage: verify-mac.sh
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
cd "$HERE"
./macos/Voice/scripts/build.sh
open macos/Voice/build/Voice.app
sleep 1.5
OUT="/tmp/voice-$(date +%H%M%S).png"
screencapture -x "$OUT"
echo "Built + launched. Screenshot: $OUT"
echo "In pi: @${OUT} \"verify Voice panel layout\""
