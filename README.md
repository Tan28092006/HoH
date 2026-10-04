# Harness Control Plane

Native worker supervision and crash recovery are documented in
[RUNTIME_OPERATIONS.md](docs/RUNTIME_OPERATIONS.md). The supervisor runs on
Windows without Docker/WSL; PostgreSQL may be native or remote.

A runnable MVP for coordinating independent Planner, Developer, QA and Integrator agents through a shared MCP control plane.

The implementation includes:

- Project -> Feature -> Subfeature-ready data model -> Ticket hierarchy.
- SQLite persistence, append-only domain audit events and redacted MCP invocation logs.
- Typed feature/ticket context references.
- Role-aware discovery.
- Timed, single-writer leases and optimistic concurrency.
- Claim-time dependency and conservative write-scope conflict gates.
- Durable role hand-offs: presence, queue, fan-out offers and an outbox boundary.
- Planner-frozen test commands run by QA against clean, candidate-bound Git worktrees.
- Immutable Git candidate identity.
- Evidence-bound QA verdicts and rework context.
- Integrator-only fast-forward merge gate, with base drift returned to Developer rework.
- Read-only Jira-style Kanban UI with ticket progress, dispatch queue and Agent activity.
- Official MCP Python SDK server over stdio.
- Local role skills under `skills/`.
- Authenticated HTTP MCP, portable per-role kits and `session.bootstrap` for fresh chats.
- Optional browser QA with candidate-specific fixtures, screenshots and traces.

See the [production audit](docs/PRODUCTION_AUDIT.md) for measured improvements and
remaining scale limits, [agent kit setup](docs/AGENT_KIT.md) to onboard a new chat,
and [browser QA](docs/UI_QA.md) for candidate UI acceptance.

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

For how a long-lived Codex or Claude Code role worker waits for hand-offs,
see [docs/ROLE_WAKEUPS.md](./docs/ROLE_WAKEUPS.md).

To use this repository as the first project managed by HoH itself, follow the
role-by-role [dogfood runbook](./docs/DOGFOOD_HOH.md).

Run the test suite with the pinned interpreter and this checkout's source:

```powershell
.\scripts\test.ps1
```

Start with the [documentation map](docs/README.md) and the
[architecture map](docs/ARCHITECTURE.md).

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
`identity.get_capabilities` returns the caller's list; `mcp/tools/` defines
every tool and the role blocks that expose it.

All roles receive `identity.get_capabilities`, `work.discover`, `feature.get_context`, `ticket.get_context`, `agent.register` and `agent.heartbeat`; only execution roles receive `work.inbox`. Every execution agent calls `agent.register` once per session and `work.inbox` whenever it becomes available. A hand-off remains pending with no QA/Dev online; when one or more eligible agents check in, each gets a short-lived offer. The first successful ticket lease wins atomically and closes the other offers. Each call is persisted with the calling agent, role, tool name, opaque target reference, timestamp and outcome. Free-text content, evidence observations, branch names, commit IDs, lease IDs and credentials are not copied to the activity log.

The browser UI is observation-only: it shows the Kanban ticket state and the redacted MCP activity feed. Ticket creation, state changes and evidence submission happen only through an authenticated role MCP server. In local mode the UI uses a local read identity; a production dashboard must be placed behind an authenticated reverse proxy or session layer rather than embedding bearer tokens in browser JavaScript.

The server supports stdio and authenticated Streamable HTTP/SSE. Network clients
require configured bearer identities, TLS and a public-host allowlist.
See [deployment](docs/DEPLOYMENT.md).

## REST API

Interactive API documentation is available at <http://127.0.0.1:8765/docs>.

Local mode accepts `X-HOH-Agent-Id` and `X-HOH-Role` headers. Never expose local mode on a network interface.

Production mode is the default when `HOH_LOCAL_MODE` is unset. It ignores claimed identity headers and derives identity from bearer tokens configured through `HOH_AGENT_TOKENS_JSON`. `HOH_ADMIN_TOKEN` optionally supplies a separate admin identity. Real secrets must only be supplied at runtime.

## Current scope

The modular monolith supports SQLite and an optional PostgreSQL store, SSE board
updates, durable outbox notifications and release gates. PostgreSQL currently
serializes write transactions to preserve service invariants; multi-host execution
still needs artifact storage and worker routing. The Integrator uses a guarded
fast-forward; automatic textual conflict resolution and provider PR creation are
outside this implementation. Notification delivery requires a receiver/host adapter
to wake a stopped chat. Managed OAuth, sandboxed worker supervision, quotas,
artifact lifecycle and large-board pagination remain production work.
