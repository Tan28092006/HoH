# Codex and Claude Code wakeups

Use a wakeup only for a dedicated execution-role worker. The scheduler creates
durable offers; a wakeup merely makes the worker call `work.inbox` soon enough
to see one.

The templates below are not installed with this skill; they live in the HoH
repository, so fetch them from a checkout rather than expecting them alongside
this file.
- Codex Desktop: use a same-task heartbeat with the repository template under
  `deploy/codex/`.
- Claude Code: copy the repository template under `deploy/claude-code/` into the
  worker's `.claude/loop.md`, run `/loop 1m`, then background that same session.

On every wakeup, renew presence and check `work.inbox`. If `active_leases` is
non-empty, do not claim again; renew a ticket lease only while useful work is
actually progressing. Stop immediately when the inbox is empty. Do not turn a
polling wakeup into backlog scanning. An offer is advisory: ticket context plus
an exclusive lease are still required before any write.

Read `docs/ROLE_WAKEUPS.md` from the HoH repository for setup and the limits of
the trusted-local SQLite deployment.
