#!/bin/bash
# Dispatch one worker using the hardwired config.
# Usage: dispatch-worker.sh --title "T" --spec "..." --name short-name
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
source "$DIR/config.sh"
TITLE=""; SPEC=""; NAME=""
while [ $# -gt 0 ]; do case "$1" in
  --title) TITLE="$2"; shift 2;;
  --spec) SPEC="$2"; shift 2;;
  --name) NAME="$2"; shift 2;;
  *) echo "unknown arg $1"; exit 1;;
esac; done
[ -n "$TITLE" ] && [ -n "$SPEC" ] && [ -n "$NAME" ] || { echo "need --title --spec --name"; exit 1; }
TASK_JSON=$(orca orchestration task-create --task-title "$TITLE" --spec "$SPEC" --json)
TASK_ID=$(echo "$TASK_JSON" | jq -r .result.task.id)
echo "task=$TASK_ID"
START_JSON=$(orca orchestration worker-start --task "$TASK_ID" --worktree "$WORKTREE_MODE" --name "$NAME" --agent "$WORKER_AGENT" --setup "$SETUP" --json)
# Parse tolerantly: never let a jq shape change mask a successful dispatch (exit 3 bug). --arg passes shell var into jq.
echo "$START_JSON" | jq --arg tid "$TASK_ID" '{dispatch: (.result.dispatch.id // .result.dispatchId // "unknown"), task: (.result.task.id // $tid)}' || echo "$START_JSON"
echo "next: orca orchestration check --wait --types \"worker_done,escalation,question\" --timeout-ms 900000 --json"
