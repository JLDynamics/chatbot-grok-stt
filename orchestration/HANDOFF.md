# Handoff — Orca orchestration setup (ALL projects)
Read this first. Works for any repo carrying an `orchestration/` dir. No digging.

## How it works (3 sentences)
One Run (inbox + namespace) is bound to one coordinator tab, which stays in the repo's main worktree.
The coordinator NEVER edits, investigates, tests, merges, or commits — it only creates tasks and dispatches
worker agents into child worktrees, then waits, verifies, and releases.
Per-project truth lives in that repo's `orchestration/config.sh` — read it first, never trust IDs from memory.

## Start here (every time, any project)
```
cat <repo>/orchestration/config.sh          # RUN_ID, ORCHESTRATOR_AGENT, WORKER_AGENT — source of truth
orca orchestration run-current --from <coordinator-handle> --json
orca orchestration worker-list --run <run-id> --json   # live worker state; count must match before/after any switch
```

## File map (relative to the repo root, identical in every project)
- `orchestration/config.sh` — RUN_ID, agents, flags. Edit here to swap.
- `orchestration/coordinator-prompt.md` — paste into any new coordinator tab (reads agents from config.sh).
- `orchestration/dispatch-worker.sh --title T --spec S --name n` — task + worker in one call.
- `orchestration/switch-orchestrator.sh [agent]` — new coordinator tab + rebinds the same Run.
- `orchestration/bootstrap-project.sh --repo-path P --objective O [--orchestrator a] [--worker w]` — clone setup to a new repo.
- `orchestration/switch-prompt.md` — fill-in-the-blanks order for delegating a switch.
- `AGENTS.md` — auto-loaded coordinator rules.

## Deployments (add a row per project; keep current)
| Repo | Run | Orchestrator | Worker |
|---|---|---|---|
| /Users/jack/Documents/chatbot-grok-stt | run_08babbc74a4c | claude (locked: no Edit/Write/NotebookEdit/Task) | pi @ xhigh |

## Switch WORKER agent (same project)
```
cd <repo>
# 1. edit WORKER_AGENT in orchestration/config.sh (one line)
# 2. future dispatches use it automatically — confirm worker-list count unchanged
git add orchestration/config.sh && git commit -m "Switch worker agent to <x>"
```

## Switch ORCHESTRATOR agent (same project, same Run)
```
cd <repo>
./orchestration/switch-orchestrator.sh <claude|codex|cursor>
orca terminal send --terminal <new-handle> --text "$(cat orchestration/coordinator-prompt.md)" --enter
orca orchestration run-current --from <new-handle> --json   # must show this repo's RUN_ID
```
Notes: only `claude` gets tool-stripped (COORDINATOR_FLAGS); codex/cursor run on prompt rules
(codex `--sandbox read-only` is untested with the orca CLI — do not enable blindly).
In-flight workers survive; new tab resumes with `check --all`. Never close the old tab yourself.

## Move setup to a NEW project
```
<any-repo>/orchestration/bootstrap-project.sh --repo-path /abs/new --objective "<objective>" --orchestrator <a> --worker <w>
cd /abs/new && ./orchestration/switch-orchestrator.sh <a>
# paste new coordinator-prompt.md, then in the new repo:
git add orchestration/ AGENTS.md && git commit -m "Add orchestration setup"
# add a row to the Deployments table above (in every copy you touch)
```

## Resume after exit / new session (any project)
```
# inside the coordinator tab:
orca orchestration run-use --id <run-id> --json
orca orchestration task-list --json
orca orchestration worker-list --run <run-id> --json
orca orchestration check --all --json
```

## Health check (before reporting done)
```
bash -n <repo>/orchestration/*.sh
orca orchestration worker-list --run <run-id> --json
git -C <repo> status --short   # main must be clean unless the user asked for edits
```

## Gotchas learned the hard way
- Orca CLI needs `--from <handle>` (or `--terminal`) outside a live Orca terminal; inside one it auto-detects.
- `worker-start --model/--effort` applies ONLY to claude/codex/cursor — never pi. Pi level = `~/.pi/agent/settings.json` (machine-global).
- jq `//` fallbacks must be quoted; shell `$VAR` inside jq needs `--arg`.
- `check --wait` returns the whole FIFO batch unfiltered — process ALL, then `--ack <deliveryId>`.
- `worker_done` auto-settles the task — never follow with `task-update completed`.
- Release settled workers (`worker-release`), never `terminal close`.
- Direct user words like "check/investigate" do NOT override never-self-work — translate to a worker task.
- Local commits only. NEVER push.
