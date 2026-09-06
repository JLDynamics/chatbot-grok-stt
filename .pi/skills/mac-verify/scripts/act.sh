#!/usr/bin/env bash
# Computer use via chatbot sidecar -> desktop-harness.
# Usage:
#   act.sh screenshot [AppName]
#   act.sh click "Sign in" [AppName]
#   act.sh type "hello" [AppName]
#   act.sh key return | escape | tab
#   act.sh hotkey "cmd s"
#   act.sh scroll [amount] [AppName]   (amount>0 scrolls up, default 5)
#   act.sh drag x1 y1 x2 y2
set -euo pipefail
SIDECAR="${SIDECAR:-http://127.0.0.1:7860/api}"
action="${1:-}"; shift || true
body=""
case "$action" in
  screenshot)
    app="${1:-}"; body=$(python3 -c "import json,sys; print(json.dumps({'action':'screenshot','app':sys.argv[1] or None}))" "$app") ;;
  click|type)
    text="${1:-}"; app="${2:-}"
    [ -z "$text" ] && { echo "need text" >&2; exit 1; }
    body=$(python3 -c "import json,sys; print(json.dumps({'action':sys.argv[1],'text':sys.argv[2],'app':sys.argv[3] or None}))" "$action" "$text" "$app") ;;
  key|hotkey)
    text="${1:-}"; [ -z "$text" ] && { echo "need keys" >&2; exit 1; }
    body=$(python3 -c "import json,sys; print(json.dumps({'action':sys.argv[1],'text':sys.argv[2]}))" "$action" "$text") ;;
  scroll)
    amount="${1:-5}"; app="${2:-}"
    body=$(python3 -c "import json,sys; print(json.dumps({'action':'scroll','amount':int(sys.argv[1]),'app':sys.argv[2] or None}))" "$amount" "$app") ;;
  drag)
    [ $# -ne 4 ] && { echo "usage: act.sh drag x1 y1 x2 y2" >&2; exit 1; }
    body=$(python3 -c "import json,sys; print(json.dumps({'action':'drag','coords':[float(sys.argv[1]),float(sys.argv[2]),float(sys.argv[3]),float(sys.argv[4])]}))" "$@") ;;
  *) echo "usage: act.sh screenshot|click|type|key|hotkey|scroll|drag ..." >&2; exit 1 ;;
esac
curl -sf -m 70 "$SIDECAR/desktop/act" -H "Content-Type: application/json" -d "$body"
echo
