# Start with only installed skills and an authenticated MCP

When an operator prompt names a project ID, call `session.bootstrap(project_id=...)`
with that exact ID and check `selected_project_id`, plus `role` and `project_ids`
inside `identity`, before registration or mutation. A chat's working directory
does not select a project. Never fall back to a different project or MCP role when
the expected profile is unavailable. Desktop-generated profiles are scoped to one
project and role; another concurrent worker of that role needs its own instance.

Otherwise call `session.bootstrap` without guessing a project or role. Select the intended
project if several are returned. Load this base skill and only the authenticated
role's skill. A role mismatch is a credential/configuration issue; declaring a
different role in text does not change permissions.

Read the `version` in bootstrap's `runtime` object and its capabilities after a restart or upgrade.
Resolve the current ticket packet and read its latest live typed Planner decision
from `context_entries` before acting; use its `context_snapshot_id` on
consequential writes that accept it. Ticket
version alone is insufficient: Planner may have appended a new decision without
changing ticket fields. A stale-context refusal means refresh the packet and
reassess the action, not replay the old call. Installed runtime, repository main
and the assigned worktree can be at different commits; verify the assigned base
against the current release handoff before implementation.

Register presence and check compatibility as required by the project. QA declares
only `execution_capabilities` it has verified on this machine; the project profile
is the intended surface, not proof that this particular session can use it. Strict
QA remains queued until Planner/operator approves the evidence-backed subset for
the same `machine_id`; QA cannot self-approve. Then inspect
bootstrap assignments and the role inbox. A new chat can resume the same identity
only after the earlier session has stopped. Two concurrent workers need different
credentials/identities; HoH leases are keyed by identity, not chat/session ID.
Several MCP processes for one profile can be normal client setup; their count
alone does not establish that multiple chats are doing work as that identity.

For Planner, repository/spec synchronization and the Planner control cycle are a
mandatory barrier immediately after bootstrap and before resuming an idle
`wait_id`. Restore an owned active Planner job first when needed to preserve its
lease, but never jump from “no lease” directly back to waiting without reviewing
Git/docs/spec changes and active ticket direction.

## Event-driven GUI sessions

When the operator requests continuous event-driven work, use
`work.wait_for_event(project_id=..., timeout_seconds=43200)` after handling ready
work. Keep the GUI turn open waiting for that MCP result; do not finish the turn
merely because the inbox is empty. This is an ordinary asynchronous MCP tool,
usable from Codex app and Claude Code's VS Code extension without creating a
separate CLI conversation or a recurring model task.

Immediately before an execution role waits, inspect its last terminal operation.
If a durable/reproducible error still has no Planner job, call
`work.report_failure` once with its failed job and narrow evidence references.
Do not report normal control flow (`context_stale`, lease/state conflict, empty
inbox), a still-running job or the first transient timeout. Existing QA verdict,
runner-infrastructure and merge-failure paths already notify Planner and must not
be duplicated.

The host sends a wake hint after a committed change. The open tool resolves
authorized durable work, reserves one notification recipient, and returns a
small event. The event is not ownership: refresh context, validate readiness,
then claim with the normal role tool. Keep `event_id`; acknowledge it using
`ack_event_id` on the next wait once handled or found obsolete. A disconnected
delivery is replayed until acknowledged; stale claimed offers are suppressed.

When this same chat resumes after Stop, timeout or restart, keep its original
instance. Inspect bootstrap assignments first. If `event_wait` is present in
`session.bootstrap`, pass its `wait_id` as `resume_wait_id` on the next
`work.wait_for_event`, including the owned job parameters when resuming a job.
This atomically replaces only that previously observed wait; it does not change
any ticket lease, job, worktree or identity. No separate permission is needed to
resume the operator's same chat. If the wait changed in the meantime, refresh
bootstrap once; repeated contention means stop and report actual concurrent use.
A `superseded` response means this old request must stop without retrying,
claiming or instructing the user to close the replacement chat. A different
simultaneous worker still needs its own instance.

For a durable worker job, replace repeated model polling with this same tool:
pass `job_kind` (`provision`, `submission`, `qa`, or `merge`) and `job_id`.
The host validates ownership and maintains the job's existing valid ticket
lease while connected. After the event, invoke its `poll_tool` once to retrieve
the authoritative result. Resume the correct assignment/worktree afterward.
Do not call an idle-work wait while holding unrelated unfinished work.

Codex's configured MCP tool timeout must exceed the wait; generated profiles
set 86400 seconds. Claude profiles set per-server `timeout` to 86400000 ms:
its separate idle limit otherwise aborts silent stdio calls after 30 minutes.
HoH sends MCP progress and checks the transport while waiting. These protocol
messages and server maintenance consume no model
polling turns. Timeout/reconnection can require a new turn; this is not an
unlimited background agent. Stop, closing the chat, sleeping the machine or
stopping HoH interrupts the connection; recover a stranded wait as described
above. A dead process's presence expires within 90 seconds.
Ordinary MCP cannot restart a stopped chat. Ask and stop when human input is
needed instead of hiding a question inside a long wait.

For an active assignment, resolve current ticket context and its required fields.
In supervised deployments, a queued job waits for the service-host worker pool.
Keep its ID and poll it; do not launch a duplicate local worker. Report a stale
supervisor or reconciliation-required merge to the operator. A new lease for
the same identity has a new assignment; never infer ownership from an agent
name or an old filesystem path.
Renew the existing lease or poll its recorded job; do not duplicate a claim,
submission, QA run or merge. Missing chat history does not mean the job failed.
An expired lease must be recovered through the role workflow, never by SQL.

## Workspaces

- `colocated`: the Developer must be able to access the server filesystem. Edit
  only the assigned worktree root and branch returned by provisioning. Verify
  `git rev-parse --show-toplevel`, branch and base before edits. A remote MCP URL
  does not make a returned local path accessible on another machine.
- `agent_workspace`: obtain the operator's Git remote and access credentials,
  clone on the agent host if needed, then create an isolated ticket branch/worktree
  in that clone from the recorded default branch/base. Do not request service
  worktree provisioning. Preserve dirty local work; do not reuse another session's
  checkout. Push a ticket branch and ensure the service repository has received
  that exact SHA before submitting. Pushing to a provider alone does not fetch it
  into the service. Git delivery/fetch remains an operator integration.
- If not configured, request repository setup through the authorized Planner.

Takeover always uses the new assignment. A -> B -> A can have several directories
for one identity; the current assignment decides which one is editable. Old dirty
work and untracked files are recovery material, not disposable cache. Lease expiry
does not stop an old OS process from writing: stop/revoke the earlier process before
manual recovery and never grant two sessions the same mutable workspace.

QA jobs for agent_workspace materialize the frozen SHA on the service host. Browser
tests need browser/runtime dependencies on that runner. Credentials alone do not
give a chat filesystem, shell, Git or browser tools. Report a missing capability
as an execution prerequisite, rather than claiming the implementation was tested.
