# Event-driven work in Codex app and Claude Code for VS Code

HoH 0.3.2 keeps the existing GUI conversation waiting on an asynchronous MCP
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
Claude's documented default MCP execution timeout is 100000000 milliseconds;
an operator's shorter override must be adjusted. Sources:
[Codex MCP configuration](https://learn.chatgpt.com/docs/extend/mcp) and
[Claude environment variables](https://code.claude.com/docs/en/env-vars).

The agent bootstraps the named project, registers capacity (normally 1), resumes
existing leases/jobs, then calls `work.wait_for_event`. Use its default 43200
seconds and pass the previous `ack_event_id` after handling an event. At a
queued provisioning, candidate submission, QA or merge job, specify `job_kind`
and `job_id`. The host waits for that owned job and renews its still-valid
lease while the connection remains alive; the client reads the final job once.

The local **Phiên agent** dialog shows waiting, notified, timed-out and expired
connections. It reports HoH-observed connection state, not undocumented internal
busy state from either GUI. An agent working outside HoH must not advertise
itself as waiting and available.

## Delivery and recovery

Work queues, notification deliveries, acknowledgments and short reservations
are durable. A committed event triggers a loopback UDP hint to waiting MCP
handlers. No business data or execution authority is carried by that hint.
Each handler rechecks role/project, capacity and current queue/job state.
One live recipient is reserved before waking its model; the actual ticket
lease remains the exclusive execution lock. Another agent can receive the
notification after the reservation/presence expires. Reconnection cannot revive
an older reservation already reassigned to a peer.

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

GUI chats can start from the same project folder to read context. A Developer
must move every edit/test command into the current lease's assigned worktree,
or its own checkout under `agent_workspace`. QA uses the immutable candidate and
external evidence paths. Neither an event nor a session's folder grants worktree
ownership. Keep old dirty work as recovery material.
