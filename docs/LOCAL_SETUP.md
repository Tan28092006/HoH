# Run Harness Control locally

This setup runs one shared control-plane database on a trusted local machine. Each
Planner, Developer or QA client connects to the shared HTTP MCP endpoint, and all use the
same SQLite database. On this machine that database is `D:\HoH\data\control-plane.db`,
served by the installed host in `D:\HoH-service` ([LOCAL_RUNTIME.md](LOCAL_RUNTIME.md)).

The scripts in steps 1 and 2 start a separate **development instance**. It uses
`D:\HoH\tmp\dev\control-plane.db` (override with `-DatabasePath` or
`HOH_DEV_DATABASE_PATH`) and port 8775, and refuses any path under `D:\HoH\data`
or `D:\HoH-service` and the installed host's ports 8765 and 8931. It never
touches the real database.

## 1. Bootstrap once

From PowerShell:

```powershell
cd D:\HoH
.\scripts\bootstrap-local.ps1
```

This creates `.venv-hoh` and installs the local package. It does not add credentials
or modify any agent configuration.

## 2. Start the read-only dashboard

```powershell
cd D:\HoH
.\scripts\start-dashboard.ps1
```

Open <http://127.0.0.1:8775>. The dashboard remains local (`127.0.0.1`), seeds
the `demo` project into the dev database when empty, and shows Jira-like ticket progress plus redacted
Agent activity. Keep this terminal running while you want the dashboard.

Use `-NoSeedDemo` only when a Planner will create the first project itself.

In a second PowerShell window, start the local lease scheduler:

```powershell
cd D:\HoH
.\scripts\start-scheduler.ps1
```

It runs every 30 seconds by default. A Developer lease that expires returns to
`READY`; an expired QA lease returns to `QA_READY`. It also fans durable queued
hand-offs to every matching online agent (up to the current fan-out limit).
Use `-Once` for a one-shot maintenance pass or `-IntervalSeconds 60` to change
the interval.

## 3. Attach exactly one role to each agent

Give each agent one identity and one role. Do not hand-write MCP configuration;
the generator registers both skills, uses the shared HTTP endpoint, sets a tool
timeout long enough for `work.wait_for_event`, and issues a per-identity
credential into the client configuration:

```powershell
& 'D:/HoH-service/.venv/Scripts/python.exe' -I -m hoh_control_plane.local_agent setup `
  --root 'D:/HoH-service' --project <project-id> --role developer `
  --client codex --upgrade-skills --transport shared-http
```

Use `--client claude-code` for Claude Code (CLI or the VS Code extension), and
`--instance <name>` for a second concurrent worker of the same role. The desktop
host prints a ready-made prompt per role that runs exactly this command. Restart
or reload the client afterwards so it loads the new MCP server.

For a client on another machine, or one other than Codex and Claude Code, build
a portable kit with `hoh-agent-kit` instead; see [AGENT_KIT.md](AGENT_KIT.md).

### Model per role

`--model <id>` pins the model a role runs on, where the client has a setting
for it:

- **Codex**: setup writes a `[profiles.<server-name>]` table with `model`. It
  applies when Codex is started with `--profile <server-name>`; the setup result
  names the server.
- **Claude Code**: there is no per-MCP-server model. For Developer, QA and
  Integrator, setup installs a worker subagent,
  `~/.claude/agents/<server-name>-worker.md`, whose `model` field applies to
  every ticket the dispatcher delegates to it; the dispatcher chat itself stays
  on whatever model it was started with. For Planner it writes nothing and says
  so in the result (`model_note`): pick the Planner chat's model with `/model`
  or `claude --model`.

Setup refuses to overwrite a different existing profile or worker file; review
and remove it first. Recommendations:

- Give **Planner the strongest model** available. A weak planner is the
  bottleneck of the whole team (Plan-and-Act): every execution role inherits its
  tickets, contracts and routing decisions.
- Give **QA a different model from Developer**. A checker that shares the
  maker's model shares its blind spots; generator/evaluator independence is
  the point of a separate QA role.
- Developer and Integrator can use a faster model: the contract, the frozen
  candidate and QA bound what they can get wrong.

## 4. Smoke test each agent

Ask each agent to call `session.bootstrap` first. Each Developer and QA
then calls `agent.register` with its installed role skill and `work.inbox` before
using `work.discover`. QA also supplies only verified `execution_capabilities`
from the project profile. Planner/operator separately approves the public
preflight-backed subset for that `machine_id`; both sets are checked at dispatch
and claim time.
`agent.heartbeat` renews availability; it is distinct
from `work.heartbeat`, which renews an active ticket lease.

- Planner should see only `planner.*` mutation tools.
- Developer should see `work.claim`, `dev.*` and no `qa.*` / `planner.*` writes.
- QA should see `qa.*`, `work.heartbeat` and no `dev.*` / `planner.*` writes.

Then reload the dashboard. The Agent activity page should show the redacted MCP
invocations, Dispatch should show presence/offer/claim state, and the Board
should reflect the ticket state. A queue remains durable if no agent is online.

For how an idle role chat waits for its next hand-off without polling, see
[ROLE_WAKEUPS.md](./ROLE_WAKEUPS.md).

## 5. Connect a real code repository

For a real project, the Planner calls `planner.configure_repository` once with
the **absolute path of that project's Git repository**. The control plane checks
that it is a Git worktree and locks that repository once any ticket worktree
exists. The source repository is never copied into the control-plane database.

The Planner then calls `planner.configure_test_command` with a list of command
arguments and a timeout. For example, a Python project might register
`["D:\\path\\to\\python.exe", "-m", "pytest", "-q"]`. This is deliberately an
argument list, not a shell string: `&&`, pipes and redirects are not executed.
The command is snapshotted into each candidate at submission, so a later project
configuration change cannot alter the test command used for an already-frozen
candidate.
The Planner reads the current command, timeout and `configured_at` from
`planner.get_test_profile` or the `test_profile` field of its
`session.bootstrap`; it never needs to open the database.

Developer flow for a configured repository is now mandatory:

1. Claim the Developer ticket.
2. Call `dev.prepare_worktree` using the lease ID and ticket version. It returns
   immediately with either a reused `worktree` or a queued `provision_id`.
3. When provisioned asynchronously, poll `dev.get_worktree_provision` with the
   same lease until status is `COMPLETED` or `FAILED`. Every poll renews the
   Developer lease.
4. Make changes only after `COMPLETED`, and only in the returned
   `worktree_path` and `branch_ref` owned by that Developer.
5. Commit the work there and submit that exact `HEAD` with `dev.submit_candidate`.

Git setup runs in a detached local worker rather than holding the MCP stdio
request open. A failed provision is durable and attributable to its lease; do
not retry blindly or reuse another Developer's worktree.

QA then claims the candidate and calls `qa.run_project_tests`. It validates the
frozen worktree, persists a durable `test_run_id`, starts a detached worker and
returns immediately; it does not hold the MCP connection while tests execute.
QA polls `qa.get_project_test_run` using that ID until it reports `COMPLETED` or
`FAILED`; each poll renews the ticket lease. Pass/fail/timeout plus bounded
stdout and stderr are written to `data\evidence` and exposed as an
`evidence://local/...` reference. A failing test is evidence; it never changes
the QA verdict automatically.

HoH stores its isolated worktrees below `data\worktrees\<project>\<ticket>\<agent>`.
It verifies, at candidate submission, that the supplied SHA is the worktree's
current branch tip and descends from the base commit captured at creation. Do not
move or manually delete those worktrees while their tickets are active.

## Local trust boundary

This is suitable when all agents run under your own local user account on one
trusted computer. The role comes from the MCP process environment, so a local
process that you control can change its own role or open the SQLite file. It is
workflow enforcement, not a hard security boundary between mutually untrusted
processes.

Do not expose the dashboard or its SQLite directory over the network. For
multiple users or machines, use the next-stage server deployment: PostgreSQL,
an authenticated HTTP MCP gateway with per-agent tokens, object storage for
evidence, and a reverse-proxied dashboard.

## What to add next

1. Platform wake adapter: the local scheduler creates offers, but an idle or
   stopped chat cannot be awakened by stdio MCP alone. Add a Codex automation,
   Claude-compatible hook, desktop notification or webhook consumer for the
   durable `dispatch_outbox` before expecting push wakeups.
2. Integration hardening: the current local Integrator only fast-forwards a
   QA-verified candidate when its base SHA still matches the checked-out
   default branch. Add post-merge mainline tests, protected-branch/PR support
   and an explicit textual-conflict workflow before relying on it for shared
   production branches.
3. PostgreSQL + authenticated remote MCP: required before agents span machines
   or user accounts.
4. SSE event stream, backups/migrations, and optional Jira synchronization.
