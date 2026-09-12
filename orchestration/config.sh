# One-line swaps live here. Everything else reads these vars.
RUN_ID="run_08babbc74a4c"
ORCHESTRATOR_AGENT="claude"
WORKER_AGENT="pi"
WORKTREE_MODE="new-child"
SETUP="run"
REPO_PATH="/Users/jack/Documents/chatbot-grok-stt"
# Coordinator launches WITHOUT file-editing tools. It only needs Bash (orca CLI) + Read.
# NOTE: not bulletproof — Bash can still write files — but removes the easy path.
# See GitHub anthropics/claude-code#31292 (conductor bypassed disallowedTools via shell).
# Coordinator tool locks. Claude: edit/subagent tools stripped at launch (hard block).
# Codex has --sandbox read-only but it is UNTESTED with orca CLI (may block the CLI
# itself) — codex/cursor coordinators run on prompt rules only until validated.
COORDINATOR_FLAGS="--disallowedTools Edit,Write,NotebookEdit,Task"
# Pi thinking for workers comes from ~/.pi/agent/settings.json defaultThinkingLevel (now xhigh).
# Orca --model/--effort only applies to claude/codex/cursor, never to pi.
