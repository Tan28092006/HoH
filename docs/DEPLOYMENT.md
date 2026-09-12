# Deployment boundaries

Use the [production audit](PRODUCTION_AUDIT.md) for known gaps and the
[portable kit](AGENT_KIT.md) for client installation. This describes implementation;
the architecture specification also includes future requirements.

## Pilot topology

One trusted execution host runs API/dashboard, HTTP MCP, scheduler, Git repositories
and detached QA/integration/provisioning workers. Keep SQLite on local disk. Back
up using SQLite's backup API and retain corresponding Git objects/evidence.
A copied live WAL database file alone is not a backup plan.

Production browser authentication needs a reverse proxy/session layer supplying
the configured REST identity. Never embed bearer tokens in JavaScript.
`HOH_LOCAL_MODE=1` trusts local identity headers and is for loopback demos only.

## Workspaces

`planner.configure_repository` records the workspace model:

| Model | Developer | QA |
|---|---|---|
| colocated | Ticket/identity/generation worktree; client needs access to the service path | Frozen candidate in the assigned worktree |
| agent_workspace | Isolated local clone/worktree; deliver the commit to the service repo | Detached checkout of the frozen SHA on the service host |

Pushing to a Git provider does not automatically fetch into the service clone.
Configure delivery/fetch and grant Git access separately. Do not push over the
service default branch: integration owns its advancement.

Bootstrap returns leases, assignments and job IDs so chats can resume without a
transcript. Stop the earlier session before reusing its identity. DB lease fencing
does not terminate an OS process or make a Git directory read-only.

## MCP transport

Stdio trusts the process owner's identity environment. Network transports use
`HOH_MCP_TOKENS_JSON`; caller-supplied role claims grant nothing. Each identity has
a role-specific tool surface. Use random 32+ ASCII character tokens without spaces,
supplied outside source control. Rotate by updating configuration and restarting.

Set `HOH_MCP_TRANSPORT=streamable-http`. Default host/port are loopback/8931. Put a
TLS proxy in front and add its public hostname to `HOH_MCP_ALLOWED_HOSTS`. Configure
exact `HOH_MCP_ALLOWED_ORIGINS` for browser clients when needed. Keep DNS rebinding
protection enabled. Both settings are comma-separated allowlists.

Streamable HTTP uses the SDK lifespan and stateless transport; task state remains
durable in the DB. New conversations initialize and call `session.bootstrap`.
This release uses bearer authentication; OAuth login/discovery is not implemented.
SSE remains available for legacy clients; prefer Streamable HTTP for new installs.

## Storage and scale

Install the `postgres` extra and set `HOH_DATABASE_URL=postgresql://...` to select
PostgreSQL for every process. `HOH_ARTIFACT_ROOT` supplies the worktree/evidence/
recovery root. Without DATABASE_URL, workers use the SQLite path. Never put SQLite
on NFS/SMB.

PostgreSQL uses a transaction advisory lock per schema for write transactions.
`BEGIN` alone does not preserve read/check/write invariants. This is a correctness
boundary and serializes writers; row/predicate locking is separate work requiring
concurrent tests. Set `HOH_TEST_POSTGRES_DSN` to a throwaway DB for conformance.
This audit environment had no running PostgreSQL server.

A DB server does not make Git/artifact paths portable. Keep execution on one host
until host affinity, Git delivery, durable artifacts and crash reconciliation are
implemented and tested. Remote agent_workspace development remains possible.

## Recovery

Run the scheduler under an OS service supervisor. Inbox polling also recovers
expired leases. Old provision/submission/QA jobs are fenced and retained as FAILED.
Poll a current job by ID instead of submitting duplicates.

A running merge whose worker disappears needs Git/DB reconciliation before retry:
the fast-forward may already have happened. Failed worktree publication can leave
an unassigned checkout; retain and inspect it before cleanup. Automatic recursive
cleanup is not implemented.

Candidate commands run as the worker OS user. For code outside a trusted team,
isolate workers with a sandbox/container/VM and minimal secrets. Removing HoH env
vars does not prevent access to home credentials, Git metadata, network or child
processes. Worker pooling, quotas and process-tree timeout remain hardening work.

Measure queue age, blocked duration, rework, integrated throughput, evidence
coverage, contention and artifact growth. Ticket counts are not delivery forecasts.
See [validation](VALIDATION.md) for actual checks performed on this release.
