# Harness Control Plane (HoH)

HoH coordinates independent AI agents working on a Git repository. It keeps
contracts, leases, immutable candidates, QA evidence and merge decisions in a
shared control plane, so a chat can resume from recorded state instead of its
transcript. A read-only dashboard shows tickets, dispatch and redacted activity.

## Four roles, one protected delivery flow

| Role | Responsibility |
| --- | --- |
| Planner | Define bounded tickets and validation contracts; reconcile repository changes and resolve exceptions. |
| Developer | Claim a lease, work in isolation and submit one immutable Git candidate. |
| QA | Independently test the frozen candidate and record findings and required evidence. |
| Integrator | Run the protected merge gate and advance the default branch when its checks pass. |

The service enforces role permissions, lease fencing, dependency and write-scope
gates, fresh context snapshots and candidate identity. QA uses a different model
family from the maker, with host-proven execution capabilities. Risk policy can
require adversarial evidence and an explicit high-risk approval.

Candidates remain frozen after submission. If the base drifts, the server can
derive a candidate on current main and run the frozen suite before landing it;
conflicts and failed checks require recovery. External landings trigger repository
reconciliation. Delivery also queues a check of main, and failures can pause the
merge queue. See [architecture and invariants](docs/ARCHITECTURE.md).

## Connect agents

New Codex and Claude Code profiles use **shared authenticated HTTP MCP** by
default: each role has its own project-scoped identity and connects to the running
host. Generate profiles with `local_agent setup` or a portable agent kit rather
than hand-writing client configuration. Restart the client after setup, then
bootstrap the role session before discovering work.

See [local setup](docs/LOCAL_SETUP.md), [portable agent kits](docs/AGENT_KIT.md)
and [role wake-ups](docs/ROLE_WAKEUPS.md). Stdio remains a compatibility option.
Remote HTTP access requires separately provisioned credentials, TLS and host
allowlists; follow [deployment boundaries](docs/DEPLOYMENT.md).

## Safe development quick start on this Windows checkout

Python 3.12 or newer is required. Bootstrap only from `D:\HoH` on `main`, with
no process using the shared development environment:

```powershell
cd D:\HoH
.\scripts\bootstrap-local.ps1
.\scripts\start-dashboard.ps1
```

Keep that terminal running. In two additional PowerShell windows:

```powershell
cd D:\HoH
.\scripts\start-scheduler.ps1
```

```powershell
cd D:\HoH
.\scripts\start-supervisor.ps1
```

Open <http://127.0.0.1:8775>. These scripts use a separate development database
under `D:\HoH\tmp\dev`, reject installed-service locations and ports, and seed
nothing unless explicitly requested. The supervisor executes queued worktree,
submission, QA and merge jobs; without it those jobs remain queued. Follow
[LOCAL_SETUP.md](docs/LOCAL_SETUP.md) for role setup and repository/test policy.

The installed Windows host is a separate runtime. Source edits do not update it.
Read [LOCAL_RUNTIME.md](docs/LOCAL_RUNTIME.md) before operating or upgrading it;
[desktop deployment](deploy/desktop/README.md) documents the launcher. Preserve
its database, credentials, candidate worktrees and evidence. Never bootstrap or
reset an existing installation as part of development setup.

## Tests

Use the wrapper to pin the interpreter and this checkout's source:

```powershell
.\scripts\test.ps1 tests\test_repo_docs.py -n 2
.\scripts\test.ps1 --fast -n 2
.\scripts\test.ps1 -n 2
```

These select targeted, fast and full lanes respectively. Only the full lane adds
the 80% coverage gate. The default worker count is six; use `-n 2` on a machine
with limited RAM or `-n 0` for serial execution. The wrapper assigns a unique
basetemp under the short `D:\.hoh-pytest` root; keep any `HOH_TEST_TEMP_ROOT`
override short and outside repositories. See [TESTING.md](docs/TESTING.md) for
lane definitions, logs and CI.

Optional PostgreSQL checks require the `postgres` extra and a disposable database
configured through `HOH_TEST_POSTGRES_DSN`. Browser checks require the `ui` extra,
Playwright and a browser installation, plus an explicit `--run-ui`; ordinary
backend runs skip them. See [browser QA](docs/UI_QA.md) for candidate isolation
and retained artifacts.

## Scope and limits

HoH is under development for a trusted execution host. SQLite is the default;
PostgreSQL is optional and serializes writes to preserve workflow invariants.
Native worker supervision supports Windows without requiring Docker or WSL.
Workers run as the host user; this is not a sandbox for untrusted code.

The autonomous crew runner is **not ready for operational use**. The documented
[Firstmate workflow](docs/FIRSTMATE.md) uses an active coordinator chat and fresh
native subagents; it does not establish an unattended crew after that chat closes.
Multi-host worker routing, shared artifact delivery, HA, managed OAuth and quotas
remain additional work. Automatic conflict resolution and provider PR creation
are outside the protected merge flow.

## Documentation

Start with the [documentation map](docs/README.md), which distinguishes current
runbooks from dated reviews. Use [ARCHITECTURE.md](docs/ARCHITECTURE.md) for code
and invariants, [RUNTIME_OPERATIONS.md](docs/RUNTIME_OPERATIONS.md) for worker
recovery, and the [release index](docs/releases/README.md) for historical changes.
[AGENTS.md](AGENTS.md) contains repository rules. The
[original specification](HOH_MULTI_AGENT_CONTROL_PLANE.md) includes unbuilt
requirements and is not a description of current implementation.
