# Switch prompt — fill the brackets, paste to any agent with shell + orca access

Goal: [SWITCH AGENTS in current project | MOVE SETUP to a new project — delete the other]

- REPO: [absolute repo path — e.g. /Users/jack/Documents/chatbot-grok-stt]
- NEW ORCHESTRATOR: [claude | codex | cursor — or KEEP]
- NEW WORKER: [pi | ... — or KEEP]
- (New project only) OBJECTIVE: [one line, e.g. "myapp: codex orchestrator plans, pi workers implement"]
- (New project only) Do NOT touch the old project's Run, tasks, or workers.

Steps:
1. Read REPO/orchestration/config.sh and REPO/orchestration/README.md first. config.sh is the source of truth.
2. SAME PROJECT: edit only the ORCHESTRATOR_AGENT / WORKER_AGENT lines in config.sh, then run `./orchestration/switch-orchestrator.sh <new-orchestrator>` from REPO. Paste REPO/orchestration/coordinator-prompt.md into the new tab via `orca terminal send --terminal <new-handle> --text "$(cat ...)" --enter`.
3. NEW PROJECT: run `REPO/orchestration/bootstrap-project.sh --repo-path <new-path> --objective "<objective>" --orchestrator <a> --worker <w>`, then `cd <new-path> && ./orchestration/switch-orchestrator.sh <a>`, paste the new coordinator-prompt.md into the new tab, and `git add orchestration/ AGENTS.md && git commit` in the new repo. Never push.
4. VERIFY and report: `bash -n` on every changed script, `orca orchestration run-current --from <new-handle>`, `task-list`, `worker-list --run <run-id>`. Running workers must be untouched — confirm their count before and after.
5. Do NOT edit any code outside orchestration/ + AGENTS.md. Do NOT close the old coordinator tab — leave that to the user.

Report back: run id, old + new terminal handles, worker counts before/after, and anything you did not do.
