# Role wakeups: Codex and Claude Code

The control plane already makes assignment durable. This document configures the
last mile: a dedicated Developer or QA chat periodically checks its own inbox,
so the agent does not need a human to say "check queue" after every hand-off.

Use one long-lived chat/session for one role and one unique `HOH_AGENT_ID`. Do
not attach both the Developer and QA MCP profiles to the same worker.

## Safety contract

Every wakeup follows this small contract:

1. Renew presence with `agent.heartbeat`. On the first run only, call
   `agent.register` with the installed role skill and capacity `1`.
2. Call `work.inbox` for the assigned project.
3. If `active_leases` is non-empty, do not claim another ticket. Resume only
   that lease's role workflow; renew its ticket lease only while doing useful
   work, never merely because the timer fired.
4. If there is no offer, end quietly. Do not scan the backlog, change a ticket,
   or start unrelated work.
5. For an offer, resolve its ticket context and claim it using the returned
   `expected_version`. A conflict means another agent won; end quietly.
6. Once a lease exists, perform only the role's normal workflow. A ticket lease
   remains the authority; a scheduled wakeup or offer is never ownership.

This makes the scheduler's durable `work_queue` and `dispatch_outbox` useful
without allowing an idle worker to invent work. A stopped machine remains
offline, and its work stays pending until another registered agent is available.

## Codex Desktop

Codex Desktop supports a recurring task attached to the current role-specific
chat. In the Developer or QA chat, after its matching HoH MCP server and skills
are installed, paste the corresponding template:

- [Developer heartbeat](../deploy/codex/hoh-developer-heartbeat.md)
- [QA heartbeat](../deploy/codex/hoh-qa-heartbeat.md)

Ask Codex to create it as a **heartbeat in this same task every minute**. A
heartbeat keeps the conversation's role and local MCP configuration; use one
minute for an active local project and increase it when idle cost matters. The
worker must run in a project-local Codex task with access to its own worktree.

Codex scheduled tasks are managed in the desktop app or ChatGPT web, rather
than Codex CLI/IDE. The official guide also distinguishes same-chat recurring
tasks from standalone tasks; HoH needs the same-chat form because the worker's
MCP identity and role context belong to that task.

## Claude Code

Claude Code can run a session-scoped loop. Configure one dedicated role session
with its matching MCP configuration, then copy the matching template into that
worker's isolated checkout as `.claude/loop.md`:

- [Developer loop prompt](../deploy/claude-code/hoh-developer-loop.md)
- [QA loop prompt](../deploy/claude-code/hoh-qa-loop.md)

In that Claude session, first call `identity.get_capabilities` and
`agent.register`. Then run `/loop 1m`, and background the session with `/bg` or
the agent view shortcut. The loop wakes the *same* session, checks its inbox and
waits silently when empty. Keep the session alive; `/loop` jobs are
session-scoped, expire after seven days, and should be recreated when needed.

Claude Code also has Channels for true event push into an already-running
session. Channels are currently a research preview and require an approved
channel plugin plus a running/background session. HoH does not enable a custom
channel automatically: it would add an external message ingress and needs a
separate authentication review. Use the loop first; add a channel only when the
team wants a managed, allowlisted push path.

## Multi-machine behavior

On the current trusted-local SQLite deployment, all workers must share the
same `D:\HoH\data\control-plane.db` over the same local machine. Do not put
the SQLite database on a network share. For multiple computers, migrate the
control plane to authenticated HTTP MCP plus PostgreSQL first; the queue,
outbox, offers and lease semantics stay unchanged.

## Operational check

1. Start the dashboard and scheduler.
2. Start one role worker and register it.
3. In Dispatch, confirm the worker appears online.
4. Let Planner release a Developer ticket or let Dev submit a candidate.
5. Confirm the target role shows an `offered` queue item, then `claimed` after
   the worker's next heartbeat.

If a worker does not respond, its presence expires and the scheduler leaves the
ticket available for another matching worker. If it claimed work but stops
heartbeating the ticket lease, the scheduler requeues that phase.
