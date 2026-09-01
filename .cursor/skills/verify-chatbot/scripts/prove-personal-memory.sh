#!/usr/bin/env bash
# Drive the personal-memory feature and write proof artifacts.
set -euo pipefail

STATE_FILE="${1:-${VERIFY_STATE_FILE:-}}"
if [[ -z "$STATE_FILE" || ! -f "$STATE_FILE" ]]; then
  echo "Usage: prove-personal-memory.sh /tmp/chatbot-verify-<run-id>/state.env" >&2
  exit 2
fi

# shellcheck disable=SC1090
source "$STATE_FILE"

FEATURE_DIR="$ARTIFACTS_DIR/personal-memory"
mkdir -p "$FEATURE_DIR"

MARKER="verify-profile-$RUN_ID"
BODY="$MARKER Verification profile for run $RUN_ID. Favorite color: cobalt."

curl -sf "$BASE_URL/api/personal-memory" >"$FEATURE_DIR/before.json"

curl -sf -X PUT "$BASE_URL/api/personal-memory" \
  -H "Content-Type: application/json" \
  -d "$(python3 -c 'import json,sys; print(json.dumps({"content": sys.argv[1]}))' "$BODY")" \
  >"$FEATURE_DIR/put-response.json"

curl -sf "$BASE_URL/api/personal-memory" >"$FEATURE_DIR/after.json"

python3 - "$FEATURE_DIR/after.json" "$MARKER" <<'PY'
import json, sys
path, marker = sys.argv[1], sys.argv[2]
data = json.load(open(path))
content = data.get("content", "")
if marker not in content:
    raise SystemExit(f"marker {marker!r} missing from saved profile")
if "cobalt" not in content:
    raise SystemExit("expected body text missing from saved profile")
print("proof_ok")
PY

cat >"$FEATURE_DIR/proof.txt" <<EOF
feature=personal-memory
entry=api+file
run_id=$RUN_ID
base_url=$BASE_URL
marker=$MARKER
EOF

echo "proof=$FEATURE_DIR"
