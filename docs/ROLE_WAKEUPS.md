# Role wakeups: Codex and Claude Code

The control plane already makes assignment durable. A role chat does not poll on
a timer: when it has nothing to do it blocks in `work.wait_for_event`, whose tool
description is the one authoritative statement of the wait protocol (timeouts,
`ack_event_id`, `resume_wait_id`, job waits). An open MCP call costs no model
turns.

Set a chat up with the generated prompt or command, never a hand-written config:

- same machine: `python -m hoh_control_plane.local_agent setup ... --transport shared-http`
  (the desktop host prints the full per-role prompt), see [LOCAL_SETUP.md](LOCAL_SETUP.md);
- another machine or client: `hoh-agent-kit`, see [AGENT_KIT.md](AGENT_KIT.md).

Both register `hoh-control-plane` plus the role skill, use the shared HTTP
endpoint and set a client tool timeout longer than the wait. Use one chat and one
identity per role; a ticket lease, never an event or offer, is ownership.
Closing the chat, Stop, sleep or stopping HoH ends the wait; reconnect and
bootstrap to resume.

## Urgent Planner intervention requests

A waiting worker learns of work when an event arrives. That
is fine for a queue, but a Developer who cannot make a safe next code action, a
QA failure, or an Integrator conflict needs a Planner sooner than the next scan.

When one of those signals opens a Planner job, the control plane also raises one
intervention request for it. The request carries pointers only - project, ticket
and context reference, event class, severity, artifact identity and evidence
references - never claims, observations, logs, secrets or lease identifiers. A
Planner reads the evidence itself through the ticket, under its own role checks.

One open Planner job has at most one live request. Repeated progress notes,
polling, queued submissions, ordinary rework and the same unresolved blocker do
not raise another. Materially new evidence updates the existing request and
makes it eligible for one more delivery.

Before becoming idle, Developer, QA or Integrator calls `work.report_failure`
only when a durable error has no Planner job yet. The service requires the
caller's live lease or its own terminal failed worker job, fingerprints the
report, blocks unsafe execution and stores only a compact context/evidence
pointer. Empty inboxes, stale context, lease/state conflicts, running jobs and a
first transient timeout are not failure feedback. QA verdict/runner failures and
terminal merge failures already open their Planner job automatically.

`python -m hoh_control_plane.planner_wakeup --database <path>` delivers pending
requests, and `scripts/start-scheduler.ps1` already runs it on each sweep.
Delivery is at-least-once and acknowledges **delivery only**: the Planner job
stays open until an authorized Planner resolves it, and no wakeup can claim a
ticket, change code or mutate project state.

Configuration:

- `HOH_PLANNER_WAKEUP_URL` - optional loopback endpoint (`127.0.0.1`,
  `localhost` or `::1`). Any other host is refused rather than called, because
  this pilot has no authenticated multi-machine transport. Redirects are refused
  too: an allowlisted endpoint answering 3xx cannot send the wakeup on to an
  unvalidated host, since checking only the configured URL would otherwise let a
  redirect escape the allowlist.
- `HOH_PLANNER_WAKEUP_SECRET` - **required whenever an endpoint is configured.**
  Each envelope carries an HMAC-SHA256 signature over its kind, nonce, issue time
  and payload, so a receiver can reject forged or replayed envelopes. Configuring
  an endpoint without the secret fails closed: the adapter refuses to build the
  envelope, records the attempt as failed and never calls the endpoint, so an
  unauthenticated request cannot reach a registered receiver. The request stays
  pending and the Planner heartbeat still finds it.

Codex Desktop exposes no external wake API, so **the fallback is local**: with no
endpoint configured, each request is written as a bounded JSON envelope under
`data/notifications/`. An operator, a desktop notifier or a polling worker reads
that directory. If the endpoint is offline the request simply stays pending, and
the ordinary Planner heartbeat still finds the job by polling.

Pending and delivered requests appear as read-only operational state in the
Dispatch view and through `planner.list_interventions`; a Planner may confirm
receipt with `planner.acknowledge_intervention`.

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
