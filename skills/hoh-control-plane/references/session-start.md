# Start with only installed skills and an authenticated MCP

When an operator prompt names a project ID, call `session.bootstrap(project_id=...)`
with that exact ID and check `selected_project_id`, `identity.role` and
`identity.project_ids` before registration or mutation. A chat's working directory
does not select a project. Never fall back to a different project or MCP role when
the expected profile is unavailable. Desktop-generated profiles are scoped to one
project and role; another concurrent worker of that role needs its own instance.

Otherwise call `session.bootstrap` without guessing a project or role. Select the intended
project if several are returned. Load this base skill and only the authenticated
role's skill. A role mismatch is a credential/configuration issue; declaring a
different role in text does not change permissions.

Register presence and check compatibility as required by the project. Then inspect
bootstrap assignments and the role inbox. A new chat can resume the same identity
only after the earlier session has stopped. Two concurrent workers need different
credentials/identities; HoH leases are keyed by identity, not chat/session ID.

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
