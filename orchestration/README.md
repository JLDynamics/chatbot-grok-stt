# Orchestration setup (hardwired)

- Run: `run_08babbc74a4c` — survives exits. Rebind with `run-use`.
- Orchestrator: stays in `main`. Workers: `new-child` worktrees, agent `pi` at `xhigh` (via `~/.pi/agent/settings.json`).
- Change agents by editing `config.sh` (one line), or run `switch-orchestrator.sh codex`.

## Resume after exit / new session
```
orca orchestration run-use --id run_08babbc74a4c --json   # inside the new orchestrator tab
orca orchestration task-list --json
orca orchestration worker-list --run run_08babbc74a4c --json
orca orchestration check --all --json
```

## Files
- `config.sh` — RUN_ID, ORCHESTRATOR_AGENT, WORKER_AGENT. Edit here to swap.
- `coordinator-prompt.md` — paste into orchestrator tab.
- `dispatch-worker.sh --title T --spec S --name n` — create task + Pi worker in one call.
- `switch-orchestrator.sh [agent]` — new orchestrator tab + rebind.
