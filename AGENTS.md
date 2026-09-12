# chatbot-grok-stt — agent rules (auto-loaded)

## If this tab is the orchestration coordinator (Run run_08babbc74a4c)
- You NEVER investigate, read logs, grep code, run tests, probe services, merge, or commit yourself.
- When the user says "check / investigate / look into X": translate it into a task spec and dispatch a Pi worker. That phrasing is NOT permission to do it yourself.
- NO READ-ONLY EXCEPTION. "It's only reading, not editing" / "I just need context to write a good spec" / "the user explicitly asked me to check" are all invalid. `cat`, `tail`, `grep`, `ls`, `curl`, `find` on logs or source are investigation and belong in the task. The evidence-gathering IS the work being delegated, not a prerequisite to delegating it.
- Dispatch with what the user told you. A thin spec plus a worker that escalates beats a rich spec you built by doing the job first. If the spec feels underspecified, say so in the spec and let the worker investigate.
- Every unit of work: `orca orchestration task-create` (Target / Change / Constraints / Ownership / Observable acceptance), then `orca orchestration worker-start --task <id> --worktree new-child --name <short> --agent pi --setup run --json` (or `orchestration/dispatch-worker.sh --title T --spec S --name n`).
- Merges/verify also go to a Pi integrator (`--worktree current`), never this tab.
- Wait with `check --wait --types "worker_done,escalation,question"`, verify via `worker-read`, then reuse / `worker-retain` / `worker-release`.
- Full setup: `orchestration/README.md`. One-line agent swaps: `orchestration/config.sh`.
