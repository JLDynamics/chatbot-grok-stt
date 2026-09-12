#!/bin/bash
# Clone this hardwired setup into another project.
# Usage: bootstrap-project.sh --repo-path /abs/path --objective "what the run is for" [--orchestrator claude] [--worker pi]
# Then: cd <repo> && ./orchestration/switch-orchestrator.sh <agent>, paste coordinator-prompt.md.
set -euo pipefail
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
DEST=""; OBJECTIVE=""; ORCH="claude"; WORKER="pi"
while [ $# -gt 0 ]; do case "$1" in
  --repo-path) DEST="$2"; shift 2;;
  --objective) OBJECTIVE="$2"; shift 2;;
  --orchestrator) ORCH="$2"; shift 2;;
  --worker) WORKER="$2"; shift 2;;
  *) echo "unknown arg $1"; exit 1;;
esac; done
[ -n "$DEST" ] && [ -n "$OBJECTIVE" ] || { echo "need --repo-path and --objective"; exit 1; }
[ -d "$DEST/.git" ] || { echo "not a git repo: $DEST"; exit 1; }
if ! orca repo show --repo "path:$DEST" --json >/dev/null 2>&1; then
  echo "registering repo in Orca..."
  orca repo add --path "$DEST" --json | jq -r '.result.repo.id // .result.repo.path // "registered"'
fi
mkdir -p "$DEST/orchestration"
for f in config.sh coordinator-prompt.md dispatch-worker.sh switch-orchestrator.sh README.md bootstrap-project.sh HANDOFF.md switch-prompt.md; do
  cp "$SRC_DIR/$f" "$DEST/orchestration/$f"
done
# Per-repo auto-loaded rules (generic: agents resolve via config.sh).
RUN_JSON=$(orca orchestration run-create --objective "$OBJECTIVE" --json)
RUN_ID=$(echo "$RUN_JSON" | jq -r '.result.run.id // empty')
[ -n "$RUN_ID" ] || { echo "run-create failed:"; echo "$RUN_JSON"; exit 1; }
# Patch the new copy (macOS + GNU sed compatible via .bak pattern).
sed -i.bak "s|^RUN_ID=.*|RUN_ID=\"$RUN_ID\"|" "$DEST/orchestration/config.sh"
sed -i.bak "s|^ORCHESTRATOR_AGENT=.*|ORCHESTRATOR_AGENT=\"$ORCH\"|" "$DEST/orchestration/config.sh"
sed -i.bak "s|^WORKER_AGENT=.*|WORKER_AGENT=\"$WORKER\"|" "$DEST/orchestration/config.sh"
sed -i.bak "s|^REPO_PATH=.*|REPO_PATH=\"$DEST\"|" "$DEST/orchestration/config.sh"
rm -f "$DEST/orchestration/config.sh.bak"
sed -i.bak "s|Run \`run_[a-z0-9]*\`|Run \`$RUN_ID\`|" "$DEST/orchestration/coordinator-prompt.md"
rm -f "$DEST/orchestration/coordinator-prompt.md.bak"
PROJ_NAME=$(basename "$DEST")
cat > "$DEST/AGENTS.md" <<EOF
# $PROJ_NAME — agent rules (auto-loaded)

## If this tab is the orchestration coordinator (Run $RUN_ID)
- You NEVER investigate, read logs, grep code, run tests, probe services, merge, or commit yourself.
- When the user says "check / investigate / look into X": translate it into a task spec and dispatch a worker (agent per \`orchestration/config.sh\` WORKER_AGENT). That phrasing is NOT permission to do it yourself.
- NO READ-ONLY EXCEPTION. Reading to "write a better spec" is still the delegated work, not a prerequisite.
- Every unit of work: task-create (Target / Change / Constraints / Ownership / Acceptance), then worker-start with --agent <WORKER_AGENT from config.sh> (or \`orchestration/dispatch-worker.sh --title T --spec S --name n\`).
- Merges/verify also go to a worker integrator (\`--worktree current\`), never this tab.
- Wait with check --wait, process ALL before --ack, verify via worker-read, then reuse / retain / release.
- Full setup: \`orchestration/HANDOFF.md\`. Agent swaps: \`orchestration/config.sh\` (one line).
EOF
echo "wrote $DEST/AGENTS.md"
echo "run=$RUN_ID"
echo "--- next ---"
echo "cd $DEST && ./orchestration/switch-orchestrator.sh $ORCH"
echo "then paste orchestration/coordinator-prompt.md into the new tab"
