# Event-driven work in Codex app and Claude Code for VS Code

HoH 0.3.3 keeps the existing GUI conversation waiting on an asynchronous MCP
call. It does not spawn a replacement CLI conversation. When ready work or a
queued job result exists, that call returns and the client can continue its
current model turn. The machine, application and waiting conversation must
remain open. Stopping a conversation cancels this mechanism.

## Start a GUI session

Select a project in the local dashboard, open **Prompt cho agent**, assign a
unique instance such as `codex-dev-1` or `claude-qa-1`, apply it, choose the role,
and copy its prompt into the corresponding GUI chat. Reload the client MCP
configuration after installation. The setup helper runs Python once to install
the profile; it does not require the user to switch to a CLI workflow.

Profiles bind identity, project and role. Codex receives `tool_timeout_sec =
86400`; otherwise its short default would turn the wait into repeated errors.
Claude has separate overall and idle timers; a silent stdio call is subject to
a 30-minute idle timeout even when its overall timeout is much longer. The kit
sets per-server `timeout = 86400000` milliseconds and preserves higher existing
values. HoH sends progress notifications when supported; stdio pings detect a
lost client and HTTP waits check their own response stream. Sources:
[Codex MCP configuration](https://learn.chatgpt.com/docs/extend/mcp) and
[Claude environment variables](https://code.claude.com/docs/en/env-vars).

The agent bootstraps the named project and registers capacity (normally 1).
Execution roles resume existing leases/jobs before accepting new work. Planner
first completes its repository/spec/ticket control cycle; an idle `wait_id` never
skips that barrier. Only after role-specific ready work is handled does the agent
call `work.wait_for_event`. Use its default 43200
seconds and pass the previous `ack_event_id` after handling an event. At a
queued provisioning, candidate submission, QA or merge job, specify `job_kind`
and `job_id`. The host waits for that owned job and renews its still-valid
lease while the connection remains alive; the client reads the final job once.
Before an execution role enters the idle wait, any durable terminal failure that
has not already produced a Planner job is sent once with `work.report_failure`.
Normal conflicts, empty queues, transient timeouts and running jobs do not create
failure context or wake Planner.

The local **Phiên agent** dialog shows waiting, notified, timed-out and expired
connections. It reports HoH-observed connection state, not undocumented internal
busy state from either GUI. An agent working outside HoH must not advertise
itself as waiting and available.

## Delivery and recovery

After restarting or resuming the same chat, keep its original instance. Bootstrap
now includes that actor's `event_wait`, with its wait ID and state. Pass the exact
observed ID as `resume_wait_id` on the next wait to replace it atomically. A stale
resume ID cannot replace a newer generation. The old request promptly returns
`superseded` and must stop; it cannot deliver work, renew a job lease or expire the
replacement's presence. No ticket, worktree, job or agent identity is reassigned.
Another simultaneous worker needs another instance. Multiple idle MCP processes
for one profile are normal in some clients and do not prove concurrent work.

This resolves a missing lifecycle distinction in 0.3.2: that version could retain
a long wait after a client-side timeout and only report an identity conflict on
retry. Its cancellation audit also used an unsupported outcome; cancellation is
now recorded as a failed call with `request_cancelled`, preserving the real cause.

Work queues, notification deliveries, acknowledgments and short reservations
are durable. A committed event triggers a loopback UDP hint to waiting MCP
handlers. No business data or execution authority is carried by that hint.
Each handler rechecks role/project, capacity and current queue/job state.
One live recipient is reserved before waking its model; the actual ticket
lease remains the exclusive execution lock. Another agent can receive the
notification after the reservation/presence expires. Reconnection cannot revive
an older reservation already reassigned to a peer.

An idle Planner also performs a throttled remote-ref observation inside the
server-side wait. This fetch does not invoke the model. A changed remote head
returns one durable `repository_changed` event with a typed observation reference;
the Planner then reviews relevant Git/docs/spec changes before waiting again.
This gives the local pilot bounded-delay external-push detection without a cron
conversation. A provider webhook remains the production path for immediate push
delivery across machines.

Unacknowledged eligible events replay with the same delivery ID. Events already
claimed by another agent are suppressed. A backend recovery read every 20
seconds catches missed hints and maintains presence; this invokes no model.
Normal event delivery does not wait for the scheduler's 30-second sweep.
The scheduler still handles lease expiry and maintenance, also without model
calls. Native TCP/HTTP timeouts, application closure and host failure still
require reconnection; this is not a guarantee of zero tokens over unlimited
idle time, nor a way to restart a stopped GUI conversation.

The same MCP request/response works for remote clients against a central service
host. The wake hint stays on that host; it is not sent directly across machines.
The local trial keeps both listeners on loopback. Multiple service hosts sharing
a database need a cross-host notification broker before claiming immediate
delivery; no such deployment is configured by this local release.

## Worktree rule

Repository validation runs off the MCP event loop. Git has detached stdin, hidden
Windows processes, bounded output and a process-tree timeout. This prevents a
Git child from holding the MCP protocol input or keeping output pipes open after
its parent times out. Incomplete output is rejected rather than used as evidence.

GUI chats can start from the same project folder to read context. A Developer
must move every edit/test command into the current lease's assigned worktree,
or its own checkout under `agent_workspace`. QA uses the immutable candidate and
external evidence paths. Neither an event nor a session's folder grants worktree
ownership. Keep old dirty work as recovery material.
