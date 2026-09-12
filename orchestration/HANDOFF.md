# Handoff — orchestration setup for chatbot-grok-stt
Read this first. Everything needed to operate or switch this setup is below. No digging.

## Live state (as of 2026-09-12T19:14Z — re-check counts before acting)
- Repo: `/Users/jack/Documents/chatbot-grok-stt` (repo `7a2ba87c-...`, main worktree, branch main)
- Run: `run_08babbc74a4c` (generation 2)
- Coordinator: `term_efb8c0ac-044c-440d-87a7-3c104ae362ce` — locked Claude (no Edit/Write/NotebookEdit/Task)
- Old tab `term_d6516cf8-...` is unbound history, safe to close whenever the user says so
- Agents: orchestrator `claude`, workers `pi` at `xhigh` (machine-global `~/.pi/agent/settings.json`)
- Tasks: 10 (9 completed, 1 = placeholder ready). Workers: 9, all succeeded/completed. Idle — no one in flight.

## File map (all in repo, all committed — never loose)
- `orchestration/config.sh` — SOURCE OF TRUTH: RUN_ID, ORCHESTRATOR_AGENT, WORKER_AGENT, COORDINATOR_FLAGS
- `orchestration/coordinator-prompt.md` — paste into any new coordinator tab
- `orchestration/dispatch-worker.sh --title T --spec S --name n` — task + worker in one call
- `orchestration/switch-orchestrator.sh [agent]` — new coordinator tab + rebinds Run
- `orchestration/bootstrap-project.sh --repo-path P --objective O [--orchestrator a] [--worker w]` — clone setup to a new repo
- `orchestration/switch-prompt.md` — fill-in-the-blanks order for delegating a switch
- `AGENTS.md` — auto-loaded coordinator rules (never self-work, never investigate)

## Switch WORKER agent (same project)
```
cd /Users/jack/Documents/chatbot-grok-stt
# 1. edit WORKER_AGENT in orchestration/config.sh (one line)
# 2. future dispatches use it automatically — nothing running to migrate (check worker-list first)
git add orchestration/config.sh && git commit -m "Switch worker agent to <x>"
```

## Switch ORCHESTRATOR agent (same project, same Run)
```
cd /Users/jack/Documents/chatbot-grok-stt
./orchestration/switch-orchestrator.sh <claude|codex|cursor>
# paste orchestration/coordinator-prompt.md into the new tab:
orca terminal send --terminal <new-handle> --text "$(cat orchestration/coordinator-prompt.md)" --enter
# verify: orca orchestration run-current --from <new-handle> --json  (must show run_08babbc74a4c)
```
Notes: only `claude` gets tool-stripped (COORDINATOR_FLAGS); codex/cursor run on prompt rules
(codex `--sandbox read-only` is untested with the orca CLI — do not enable blindly).
In-flight workers survive the swap; new tab resumes with `check --all`.

## Move setup to a NEW project
```
./orchestration/bootstrap-project.sh --repo-path /abs/new --objective "<objective>" --orchestrator <a> --worker <w>
cd /abs/new && ./orchestration/switch-orchestrator.sh <a>
# paste new coordinator-prompt.md, then in new repo:
git add orchestration/ AGENTS.md && git commit -m "Add orchestration setup"
```

## Resume after exit / new session (same project)
```
# inside the coordinator tab:
orca orchestration run-use --id run_08babbc74a4c --json
orca orchestration task-list --json
orca orchestration worker-list --run run_08babbc74a4c --json
orca orchestration check --all --json
```

## Health check (run before reporting done)
```
bash -n orchestration/*.sh
orca orchestration worker-list --run run_08babbc74a4c --json   # worker count must match before/after any switch
git -C /Users/jack/Documents/chatbot-grok-stt status --short  # main must be clean unless user asked for edits
```

## Gotchas learned the hard way
- Orca CLI needs `--from <handle>` (or `--terminal`) when run outside a live Orca terminal; inside one it auto-detects.
- `worker-start --model/--effort` applies ONLY to claude/codex/cursor — never pi. Pi level = `~/.pi/agent/settings.json`.
- jq `//` fallbacks must be quoted; `$VAR` inside jq needs `--arg` (exit-3-after-success bug, fixed).
- `check --wait` returns the whole FIFO batch unfiltered — process ALL, then `--ack <deliveryId>`.
- `worker_done` auto-settles the task — never follow with `task-update completed`.
- Release settled workers (`worker-release`), never `terminal close`.
- Direct user words like "check/investigate" do NOT override the never-self-work rule — translate to a Pi task.
- Local commits only. NEVER push.
