# Run Harness Control locally

This setup runs one shared control-plane database on a trusted local machine. Each
Planner, Developer or QA client starts its own MCP stdio process, but all use the
same SQLite database at `D:\HoH\data\control-plane.db`.

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

Open <http://127.0.0.1:8765>. The dashboard remains local (`127.0.0.1`), seeds
the `demo` project when empty, and shows Jira-like ticket progress plus redacted
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

Do not configure all three role templates on the same autonomous agent. Give each
agent one identity and one role:

| Agent | MCP template | Local skill folders |
| --- | --- | --- |
| Planner | `deploy/codex/hoh-planner.toml` or `deploy/claude-code/hoh-planner.mcp.json` | `skills/hoh-control-plane`, `skills/hoh-planner` |
| Developer | `deploy/codex/hoh-developer.toml` or `deploy/claude-code/hoh-developer.mcp.json` | `skills/hoh-control-plane`, `skills/hoh-developer` |
| QA | `deploy/codex/hoh-qa.toml` or `deploy/claude-code/hoh-qa.mcp.json` | `skills/hoh-control-plane`, `skills/hoh-qa` |

Every template assumes this exact repository location. If it moves, replace all
`D:\HoH` values with the new absolute path. Assign a unique `HOH_AGENT_ID` to
each running client. Keep `HOH_PROJECT_IDS=demo` for the seeded demo; change it
to a real project ID after planning it. A Planner that creates the very first
project needs `HOH_PROJECT_IDS=*` for that initial bootstrap only.

Install the role-specific skill pair into the matching client as well. For example:

```powershell
# One Developer instance using Codex:
.\scripts\install-agent-skill.ps1 -Target Codex -Role developer

# One QA instance using Claude Code, scoped only to this repository:
.\scripts\install-agent-skill.ps1 -Target ClaudeCode -Role qa -Scope project
```

The installer copies only `hoh-control-plane` plus the role skill. Restart that
agent client after copying. Use a different role command for every separate
agent instance.

### Codex Desktop, CLI or IDE

Merge only the matching file under `deploy/codex/` into that agent's
`%USERPROFILE%\.codex\config.toml`, then restart the Codex client. Alternatively,
add the command with the Codex MCP UI or CLI. The command is:

```powershell
codex mcp add hoh-developer `
  --env HOH_DATABASE_PATH=D:\HoH\data\control-plane.db `
  --env HOH_AGENT_ID=developer-01 `
  --env HOH_AGENT_ROLE=developer `
  --env HOH_PROJECT_IDS=demo `
  -- powershell.exe -NoProfile -ExecutionPolicy Bypass -File D:\HoH\scripts\start-mcp.ps1
```

Verify it with `codex mcp list`, then use `/mcp` inside Codex. Codex shares its
MCP configuration across Desktop, CLI and IDE clients. See the official
[Codex MCP documentation](https://learn.chatgpt.com/docs/extend/mcp?surface=cli).

### Claude Code

For a Developer agent, add the matching stdio process (adapt the other role
templates in the same way):

```powershell
claude mcp add --scope user `
  --env HOH_DATABASE_PATH=D:\HoH\data\control-plane.db `
  --env HOH_AGENT_ID=developer-01 `
  --env HOH_AGENT_ROLE=developer `
  --env HOH_PROJECT_IDS=demo `
  hoh-developer `
  -- powershell.exe -NoProfile -ExecutionPolicy Bypass -File D:\HoH\scripts\start-mcp.ps1
```

Run `claude mcp list` to verify. The JSON templates in `deploy/claude-code/`
are useful for a project-scoped configuration. Claude Code documents local
stdio servers, environment variables and `local` / `project` / `user` scopes in
its [MCP guide](https://docs.anthropic.com/en/docs/claude-code/mcp).

### Claude Code VS Code extension

The extension reads the same project-scoped `.mcp.json` file, so it does not
need a separate MCP server implementation or a CLI worker. Copy the matching
template from `deploy/claude-code/` to the repository root as `.mcp.json`, then
set a unique `HOH_AGENT_ID`, the fixed role, and the project's exact scope.
For the HoH dogfood project, the checked-out root `.mcp.json` is already a
Developer profile with `developer-claude-01` scoped only to
`hoh-control-plane`.

Reload the VS Code window after changing the file. In the Claude Code chat,
open `/mcp` and approve `hoh-developer` when prompted; project-scoped MCP
servers require this one-time approval. Confirm that the tool list contains
Developer tools (`work.claim`, `dev.*`) and does not contain Planner or QA
mutation tools. Do not open a Planner, QA, and Developer profile in the same
Claude chat.

## 4. Smoke test each agent

Ask each agent to call `identity.get_capabilities` first. Each Developer and QA
then calls `agent.register` with its installed role skill and `work.inbox` before
using `work.discover`. `agent.heartbeat` renews availability; it is distinct
from `work.heartbeat`, which renews an active ticket lease.

- Planner should see only `planner.*` mutation tools.
- Developer should see `work.claim`, `dev.*` and no `qa.*` / `planner.*` writes.
- QA should see `qa.*`, `work.heartbeat` and no `dev.*` / `planner.*` writes.

Then reload the dashboard. The Agent activity page should show the redacted MCP
invocations, Dispatch should show presence/offer/claim state, and the Board
should reflect the ticket state. A queue remains durable if no agent is online.

To let a dedicated Codex or Claude Code Developer/QA worker wake itself and
check the queue, follow [ROLE_WAKEUPS.md](./ROLE_WAKEUPS.md). This is optional
for manual use, but it is the recommended local setup when hand-offs should not
wait for a person to prompt an idle chat.

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

Developer flow for a configured repository is now mandatory:

1. Claim the Developer ticket.
2. Call `dev.prepare_worktree` using the lease ID and ticket version.
3. Make changes only in its returned `worktree_path` and `branch_ref`.
4. Commit the work there and submit that exact `HEAD` with `dev.submit_candidate`.

QA then claims the candidate and calls `qa.run_project_tests`. It can execute
only that candidate's frozen Planner command, after verifying that the assigned
worktree is clean and still at the frozen SHA. Pass/fail/timeout plus bounded
stdout and stderr are written to `data\evidence` and exposed to QA as an
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
