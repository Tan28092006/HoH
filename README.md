# Harness Control Plane

A runnable MVP for coordinating independent Planner, Developer, QA and Integrator agents through a shared MCP control plane.

The implementation includes:

- Project -> Feature -> Subfeature-ready data model -> Ticket hierarchy.
- SQLite persistence, append-only domain audit events and redacted MCP invocation logs.
- Typed feature/ticket context references.
- Role-aware discovery.
- Timed, single-writer leases and optimistic concurrency.
- Durable role hand-offs: presence, queue, fan-out offers and an outbox boundary.
- Planner-frozen test commands run by QA against clean, candidate-bound Git worktrees.
- Immutable Git candidate identity.
- Evidence-bound QA verdicts and rework context.
- Read-only Jira-style Kanban UI with ticket progress, dispatch queue and Agent activity.
- Official MCP Python SDK server over stdio.
- Local role skills under `skills/`.

The full architecture is in [HOH_MULTI_AGENT_CONTROL_PLANE.md](./HOH_MULTI_AGENT_CONTROL_PLANE.md).

## Local quick start

Python 3.12 is recommended.

```powershell
cd D:\HoH
.\scripts\bootstrap-local.ps1
.\scripts\start-dashboard.ps1
```

In a second PowerShell window, keep expired leases from stalling the board and
dispatch queued work to live agents:

```powershell
.\scripts\start-scheduler.ps1
```

Open <http://127.0.0.1:8765>. The local runner enables local identity headers and seeds a demo project. To attach the role-scoped MCP to separate Codex or Claude agents, follow [docs/LOCAL_SETUP.md](./docs/LOCAL_SETUP.md).

Run the test suite:

```powershell
python -m pytest
```

## MCP server

Each agent gets its own stdio MCP process and role identity. Example environment for a Developer:

```text
HOH_DATABASE_PATH=D:\HoH\data\control-plane.db
HOH_AGENT_ID=dev-agent-03
HOH_AGENT_ROLE=developer
HOH_PROJECT_IDS=demo
PYTHONPATH=D:\HoH\src
```

Launch command:

```powershell
python -m hoh_control_plane.mcp_server
```

Use the same command with role `planner`, `qa` or `integrator` for other agents. The backend, not the skill, enforces role permissions.

The tool list is role-scoped: an agent only receives tools it is allowed to use.

| Role | MCP write tools |
| --- | --- |
| Planner | `planner.create_project`, `planner.create_feature`, `planner.create_ticket`, `planner.set_ticket_ready`, `planner.append_context` |
| Developer | `agent.register`, `agent.heartbeat`, `work.inbox`, `work.claim`, `work.heartbeat`, `dev.append_progress`, `dev.report_blocker`, `dev.submit_candidate` |
| QA | `agent.register`, `agent.heartbeat`, `work.inbox`, `qa.claim_candidate`, `work.heartbeat`, `qa.record_test_run`, `qa.submit_verdict` |

All roles receive `identity.get_capabilities`, `work.discover`, `feature.get_context`, `ticket.get_context`, `agent.register` and `agent.heartbeat`; only execution roles receive `work.inbox`. Every execution agent calls `agent.register` once per session and `work.inbox` whenever it becomes available. A hand-off remains pending with no QA/Dev online; when one or more eligible agents check in, each gets a short-lived offer. The first successful ticket lease wins atomically and closes the other offers. Each call is persisted with the calling agent, role, tool name, opaque target reference, timestamp and outcome. Free-text content, evidence observations, branch names, commit IDs, lease IDs and credentials are not copied to the activity log.

The browser UI is observation-only: it shows the Kanban ticket state and the redacted MCP activity feed. Ticket creation, state changes and evidence submission happen only through an authenticated role MCP server. In local mode the UI uses a local read identity; a production dashboard must be placed behind an authenticated reverse proxy or session layer rather than embedding bearer tokens in browser JavaScript.

The server follows the official Python SDK's stdio transport for local hosts. A future deployment adapter can mount Streamable HTTP with production token validation.

## REST API

Interactive API documentation is available at <http://127.0.0.1:8765/docs>.

Local mode accepts `X-HOH-Agent-Id` and `X-HOH-Role` headers. Never expose local mode on a network interface.

Production mode is the default when `HOH_LOCAL_MODE` is unset. It ignores claimed identity headers and derives identity from bearer tokens configured through `HOH_AGENT_TOKENS_JSON`. `HOH_ADMIN_TOKEN` optionally supplies a separate admin identity. Real secrets must only be supplied at runtime.

## Current scope

This MVP deliberately uses a modular monolith and SQLite so the core workflow is easy to inspect and test. It has a durable outbox but no adapter that can wake a stopped/idle Claude or Codex chat: that requires a platform-specific webhook, automation or desktop notification adapter. PostgreSQL, object storage, Jira synchronization, server-side event streaming and managed OAuth are subsequent phases. The schema and service boundaries are structured so those adapters can be added without changing the agent workflow contract.
