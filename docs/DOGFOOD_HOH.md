# Dogfood HoH on HoH

Use the Harness Control Plane to develop this repository itself. This is a
local, controlled first run: Planner creates a bounded backlog from the source
plan, Developer implements one leased ticket, and QA independently verifies its
frozen candidate.

Do not delete the seeded `demo` project. This run creates a separate
`hoh-control-plane` project in the same local database.

## Boundary for the first run

Use one role per agent session. The local deployment is a trusted single-user
workflow boundary, not security isolation between hostile processes.

Codex Desktop, Codex CLI, and the Codex IDE extension on the same host share
their MCP configuration. Therefore do not enable Planner, Developer, and QA
MCP profiles together and treat that as a hard role boundary. For this pilot:

1. Enable the Planner profile and create the backlog.
2. Disable or remove the Planner profile, then enable the Developer profile.
3. Use a separate QA session after a Developer candidate exists. Claude Code is
   a convenient separate client; otherwise switch the active Codex profile.

For simultaneous workers or untrusted team members, use separate machines or
OS users until the planned authenticated HTTP MCP and PostgreSQL deployment is
available.

## 1. Start the local control plane

Run these commands in separate PowerShell windows:

```powershell
cd D:\HoH
.\scripts\bootstrap-local.ps1
.\scripts\start-dashboard.ps1
```

```powershell
cd D:\HoH
.\scripts\start-scheduler.ps1
```

Open <http://127.0.0.1:8765>. The scheduler keeps expired leases and pending
role hand-offs from stalling the board.

## 2. Start the Planner worker

Install the two Planner skills, then add only the Planner MCP profile to the
Planner client:

```powershell
cd D:\HoH
.\scripts\install-agent-skill.ps1 -Target Codex -Role planner -Scope project

codex mcp add hoh-planner `
  --env HOH_DATABASE_PATH=D:\HoH\data\control-plane.db `
  --env HOH_AGENT_ID=planner-hoh-01 `
  --env HOH_AGENT_ROLE=planner `
  --env "HOH_PROJECT_IDS=*" `
  -- powershell.exe -NoProfile -ExecutionPolicy Bypass -File D:\HoH\scripts\start-mcp.ps1

codex mcp list
```

Restart Codex, open a new task rooted at `D:\HoH`, and use `/mcp` to confirm
that `hoh-planner` is connected. The wildcard scope is only for the initial
creation of the project. It will be reduced to `hoh-control-plane` afterwards.

Give the Planner this prompt:

```text
You are the Planner for the HoH Control Plane project itself. Load the
hoh-control-plane and hoh-planner skills. First call identity.get_capabilities.

Treat HOH_MULTI_AGENT_CONTROL_PLANE.md as the source product plan and inspect
the current repository to distinguish shipped behavior from planned behavior.
Create project_id `hoh-control-plane`, configure repository `D:\HoH`, and
configure the test command
["D:\\HoH\\.venv-hoh\\Scripts\\python.exe", "-m", "pytest", "-q"] with a
900-second timeout.

Create a feature tree and bounded Developer tickets for only the highest-value
unimplemented work. Record the planning decision with planner.append_context,
including a source reference to HOH_MULTI_AGENT_CONTROL_PLANE.md. Every ticket
needs observable acceptance criteria, preservation constraints, dependencies,
and write scope. Do not edit code and do not create more than one READY ticket
in the first batch.

Prioritize the workflow-correctness gaps: enforce dependency readiness in the
claim path, enforce file-scope conflict policy, and create a Planner work-loop
from QA/blocker evidence. Present the proposed backlog before releasing the
first ticket. After approval in this task, release only that one ticket.
```

The intentional first-ticket candidates are:

| Priority | Ticket outcome |
| --- | --- |
| P0 | Reject a direct Developer claim if a prerequisite ticket is not `DONE`. |
| P0 | Reject or serialize a Developer claim whose active ticket has an overlapping write scope. |
| P1 | Add a leased Planner work item that converts blockers and QA evidence into bounded planning work. |

The Planner may refine this order after it reads current state; it must not
assume the design document is already implemented.

## 3. Start the Developer worker

After the Planner creates `hoh-control-plane`, remove or disable the temporary
Planner MCP server in the Codex MCP settings. Then install and add the
Developer profile, scoped to the real project:

```powershell
cd D:\HoH
.\scripts\install-agent-skill.ps1 -Target Codex -Role developer -Scope project

codex mcp add hoh-developer `
  --env HOH_DATABASE_PATH=D:\HoH\data\control-plane.db `
  --env HOH_AGENT_ID=developer-hoh-01 `
  --env HOH_AGENT_ROLE=developer `
  --env HOH_PROJECT_IDS=hoh-control-plane `
  -- powershell.exe -NoProfile -ExecutionPolicy Bypass -File D:\HoH\scripts\start-mcp.ps1
```

Restart Codex and open a separate Developer task. Use this prompt:

```text
You are the Developer worker for `hoh-control-plane`. Load the
hoh-control-plane and hoh-developer skills. Confirm identity first, register
as capacity 1, and call work.inbox. If offered work exists, read its feature
and ticket context before claiming it. Otherwise discover only ready work.

Work on exactly one lease. Prepare the assigned Git worktree, make a small
implementation with tests, append concise progress, commit the worktree, and
submit its exact HEAD as the candidate. Do not change ticket planning, do not
perform QA acceptance, and do not merge.
```

The Board should show `DEV_IN_PROGRESS`, then `QA_READY` after candidate
submission. The Dispatch view records the hand-off; Agent activity records the
redacted MCP calls.

## 4. Start the QA worker

Use a separate Codex/Claude session with only the QA MCP profile and its two
skills. The Codex command is the same shape as above, with:

```text
HOH_AGENT_ID=qa-hoh-01
HOH_AGENT_ROLE=qa
HOH_PROJECT_IDS=hoh-control-plane
```

Then use this prompt:

```text
You are the independent QA worker for `hoh-control-plane`. Load the
hoh-control-plane and hoh-qa skills. Confirm identity, register as capacity 1,
and call work.inbox. Claim only an offered QA candidate. Read the ticket
context, start qa.run_project_tests against the frozen candidate, and poll
qa.get_project_test_run until the durable test run reaches a terminal status.
Then record criterion-level evidence and submit PASS or FAIL. Do not edit code,
repair the candidate, alter ticket scope, or merge.
```

On FAIL, stop the QA task and let a new Developer turn obtain the rework
ticket. On PASS, the control plane creates a durable Integrator hand-off.

## 5. Integrate the verified candidate

Use an isolated Integrator session and add its sole MCP profile:

```powershell
codex mcp add hoh-integrator `
  --env HOH_DATABASE_PATH=D:\HoH\data\control-plane.db `
  --env HOH_AGENT_ID=integrator-hoh-01 `
  --env HOH_AGENT_ROLE=integrator `
  --env HOH_PROJECT_IDS=hoh-control-plane `
  -- powershell.exe -NoProfile -ExecutionPolicy Bypass -File D:\HoH\scripts\start-mcp.ps1
```

Use this prompt:

```text
You are the Integrator for `hoh-control-plane`. Confirm identity, register as
capacity 1, and use work.inbox to obtain only a verified candidate. Read ticket
context and claim it with integrator.claim_candidate. Then call
integrator.merge_candidate exactly once. Do not edit the candidate or resolve
conflicts manually in this phase.

If the result is merged, confirm the ticket is DONE. If the result is
base_drift, leave the rework ticket for Developer; do not force-merge or reset
main.
```

The local merge gate only fast-forwards when `main` still equals the candidate's
recorded base SHA. A changed mainline returns the ticket to `REWORK`, with
integration context for the next Developer. This is intentionally conservative:
automatic rebase/conflict resolution and post-merge mainline testing are later
hardening work.

## 6. After the first loop

Change the Planner's scope from `*` to `hoh-control-plane`. Keep all project
mutations through role MCP tools; the browser UI is observation-only. When a
Developer or QA worker should poll automatically, add the matching same-session
heartbeat in [ROLE_WAKEUPS.md](ROLE_WAKEUPS.md). Do not rely on it for push
delivery: stopped sessions remain represented by a pending durable queue item.

Before adding another machine, complete the remote foundation work: PostgreSQL,
authenticated HTTP MCP, per-agent credentials, a real outbox delivery adapter,
and backups. Do not put `control-plane.db` on a network share.
