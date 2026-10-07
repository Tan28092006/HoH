# Deploying the control plane across machines

Start with [current deployment boundaries](../docs/DEPLOYMENT.md). A shared
database alone does not make local worktrees, evidence or worker processes portable.

The control plane assumes one thing about its network: that it is not on one.
Every process binds loopback, every credential is a bearer token, and nothing
here terminates TLS. That is deliberate — a reverse proxy does TLS better than
an application does — but it means a deployment is not finished until the proxy
is in front of it.

## What has to be true before agents on other machines connect

1. **A shared database.** SQLite is a file; two machines cannot both write it
   safely. Set `HOH_DATABASE_URL` to a PostgreSQL URL and every process — the
   MCP endpoint, the scheduler, the workers — opens the same database.
2. **TLS in front of the MCP endpoint.** Tokens are bearer credentials: whoever
   reads one off the wire *is* that agent until it is revoked.
3. **Tokens issued per agent**, not shared. The identity is server-side data the
   caller never supplies, so a token is the whole of the claim.

## Configuration

| Variable | Meaning |
| --- | --- |
| `HOH_DATABASE_URL` | `postgresql://…`. When set, wins over the path below. |
| `HOH_DATABASE_PATH` | SQLite file. Local installs only. |
| `HOH_ARTIFACT_ROOT` | Where worktrees, evidence and patches live. A server-backed store has no directory of its own, so state this. |
| `HOH_MCP_TRANSPORT` | `stdio` (default), `streamable-http`, or `sse`. Listening on a network is an explicit decision. |
| `HOH_MCP_HOST` / `HOH_MCP_PORT` | Defaults `127.0.0.1:8931`. **Leave the host on loopback** and let the proxy reach it. |
| `HOH_MCP_TOKENS_JSON` | Token → identity map. Required for any network transport. |
| `HOH_WAKEUP_SECRET` | Signs wakeup envelopes. **Unset means no wakeups are sent at all** — deliberately silent, so that setting it later starts delivery instead of finding the retry budget already spent. |
| `HOH_WAKEUP_ALLOWED_HOSTS` | Comma-separated hosts that may receive a wakeup, *added* to loopback. Anything off-machine must be `https`. |

### Issuing tokens

`HOH_MCP_TOKENS_JSON` maps a secret to the identity it authenticates as:

```json
{
  "<32+ character token>": {
    "agent_id": "dev-01",
    "role": "developer",
    "project_ids": ["hoh-control-plane"]
  }
}
```

Generate one per agent, never reuse:

```sh
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Tokens shorter than 32 characters are refused at startup — a guessable token
makes every other control here decorative. Roles are `planner`, `developer`,
`qa`, `integrator`, `admin`. `project_ids` is the scope; there is no wildcard
worth using in a deployment.

Each credential gets its own MCP server instance bound to its own identity, so a
role's tools are **physically absent** from the surface its token reaches rather
than merely refused when called. To rotate, add the new token, move the agent,
then drop the old entry and restart.

### Evidence storage must be reachable from every machine

`HOH_ARTIFACT_ROOT` is where evidence, worktrees and recovery patches live. QA
writes a report there and records it as `evidence://local/<candidate>/<run>.json`;
the Integrator later resolves that same reference against its own artifact root.
**If the two machines do not see the same storage, that resolution finds nothing.**

The code puts worktrees and evidence below the same artifact root. Use one
execution host until storage and host-affinity adapters separate those concerns.
Sharing the whole root is not a supported way to scale Git worktrees.
A remote Developer can instead use agent_workspace.

Failing this does not raise. When the artifact is absent, the digest falls back
to covering the retained record, which is the QA agent's claim about a file it
holds elsewhere — a legitimate path for evidence genuinely kept off-box, and
indistinguishable from misconfiguration if nothing counts it. So it is counted:
`evidence.artifact_backed_ratio` in the project metrics is the number to watch,
and a deployment where it falls toward zero has storage that no longer reaches
the machines writing to it.

### Reverse proxy

[`caddy/Caddyfile`](caddy/Caddyfile) and [`nginx/hoh.conf`](nginx/hoh.conf) are
working starting points. Caddy obtains and renews certificates itself, which is
one fewer thing to forget; nginx is there for deployments that already run it.

Both do the same three things: terminate TLS, pass the `Authorization` header
through untouched, and leave response buffering off so the event stream is not
held back.

## Processes to run

```sh
hoh-mcp        # the MCP endpoint agents connect to
hoh-supervisor # runs the queued worker jobs (worktrees, submissions, QA runs, merges)
hoh-scheduler  # lease recovery, work dispatch, and wakeup delivery
hoh-api        # the operator UI and REST surface (optional)
```

`HOH_WORKER_MODE` defaults to `supervised`: `hoh-mcp` only queues jobs, and
without `hoh-supervisor` they stay queued. Run exactly one supervisor per
artifact root ([RUNTIME_OPERATIONS.md](../docs/RUNTIME_OPERATIONS.md)).

`hoh-scheduler` is not optional in a real deployment. It is what recovers an
expired lease, what dispatches queued work, and what knocks on an agent to say
the work is there. Without it, work still flows — every agent polls — but
nothing is ever recovered from a crashed agent.

## Upgrading

Serving processes (`hoh-mcp`, `hoh-api`, `hoh-scheduler`, `hoh-supervisor`)
refuse a database an older release wrote, untouched, with
`schema_upgrade_required`; none of them upgrades it. Stop every process that
uses the database, install the new version, then run `hoh-ops migrate`. For
SQLite it first writes an integrity-checked `sqlite3.backup()` copy
(`--backup-dir` chooses where); for PostgreSQL take your own backup and pass
`--external-backup`. Added tables, columns and indexes are declared in
`store_migrations.py` and applied by whichever store is in use.

Two migrations are SQLite-only by design. They rebuild tables to repair foreign
keys and CHECK constraints that only ever went wrong in SQLite, because it
cannot alter a constraint in place; a PostgreSQL database created from the
current schema never had the defect they undo.
