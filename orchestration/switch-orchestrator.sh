#!/bin/bash
# Switch orchestrator agent without losing the Run.
# Usage: switch-orchestrator.sh [claude|codex|cursor|pi]  (default: $ORCHESTRATOR_AGENT)
# Run this from inside the repo. Paste coordinator-prompt.md into the new tab after.
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
source "$DIR/config.sh"
AGENT="${1:-$ORCHESTRATOR_AGENT}"
# Coordinator gets no file-editing tools (hard block, not just prompt). $COORDINATOR_FLAGS from config.sh.
CMD="$AGENT"
if [ "$AGENT" = "claude" ]; then CMD="claude $COORDINATOR_FLAGS"; fi
OUT=$(orca terminal create --worktree "path:$REPO_PATH" --title "Coordinator ($AGENT)" --command "$CMD" --json)
HANDLE=$(echo "$OUT" | jq -r '.result.handle // .result.terminal.handle // empty')
if [ -z "$HANDLE" ] || [ "$HANDLE" = "null" ]; then echo "$OUT"; exit 1; fi
echo "new terminal: $HANDLE"
orca orchestration run-use --id "$RUN_ID" --from "$HANDLE" --json | jq .
echo "---"
echo "Paste orchestration/coordinator-prompt.md into the new $AGENT tab."
echo "Then resume: orca orchestration check --all --json ; orca orchestration worker-list --run $RUN_ID --json"
