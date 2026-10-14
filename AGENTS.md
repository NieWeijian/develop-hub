# Develop Hub contributor guidance

## Scope

This repository implements the 175 control plane. Keep orchestration state in the server database; never infer workflow success only from an Agent's prose response.

## Architecture boundaries

- `server/` owns workflow state, Codex process control, Git worktrees, approvals, audit events, and API contracts.
- `web/` is a presentation and operator-control surface. It must not spawn processes or derive authoritative workflow transitions.
- Codex JSONL is an external event format. Store raw events, then normalize them before the frontend consumes them.
- A Codex thread is bound to its node, role, run, and worktree. Never resume with `--last`.
- Build, test, and release success must be based on configured command exit codes and verified artifacts, not Agent self-reporting.

## Verification

- Server: `PYTHONPATH=server/src .venv/bin/python -m pytest server/tests`
- Frontend: from `web/`, run `npm run lint` and `npm run build`.
- Schema changes require an Alembic migration.

## Safety

- Never auto-merge, publish, delete a worktree, or run a release command without an explicit workflow gate.
- Keep subprocess working directories inside the registered repository or configured worktree root.
- Do not log credentials, auth tokens, or the full process environment.


## Isolated development environment (2026-10-14)

This is the active platform development checkout. Stable UI 3015/API 8185 and
`/home/nwj/develop-hub-redesign` must never be edited, rebuilt, or restarted here.
Use `/home/nwj/develop-hub-dev/ops/manage.py` for builds, tests, and dev restarts.
Development containers have no network or hardware access, independent dependencies,
an empty testing database, and simulation-only devices. Keep these restrictions.
Do not mount production directories, SSH identities, hardware, or the Docker socket.
Do not provision or reuse production test stations. Real hardware work requires a
separate authorized test station. SDK code edits must use SSH, never PowerShell/SMB.
