# Waiting for work

Do not poll. When no work is ready, wait with `work.wait_for_event` as its tool
description says, and resume a dropped wait with `resume_wait_id`. An event is
not a lease: resolve current context and claim through the role's normal tools,
then hand the claim's `worker_brief` to a fresh worker as the base card's
"One ticket, one fresh worker" section says.

On every wake, renew presence and check `work.inbox`. If `active_leases` is
non-empty, do not claim again; renew a ticket lease only while useful work is
actually progressing. Do not turn a wake into backlog scanning.

Client setup is generated, not hand-written: the HoH repository's
`local_agent setup` command (same machine) or `hoh-agent-kit` (portable kit)
writes the MCP configuration and installs these skills. A stopped chat cannot be
woken by MCP; reconnect and bootstrap to resume.
