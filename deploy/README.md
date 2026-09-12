# Deploying the control plane across machines

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

Point `HOH_ARTIFACT_ROOT` at a mount every machine shares. Worktrees are the
exception and are meant to be local — a Developer works in its own checkout and
what travels to the next role is the commit, not the tree.

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
hoh-scheduler  # lease recovery, work dispatch, and wakeup delivery
hoh-api        # the operator UI and REST surface (optional)
```

`hoh-scheduler` is not optional in a real deployment. It is what recovers an
expired lease, what dispatches queued work, and what knocks on an agent to say
the work is there. Without it, work still flows — every agent polls — but
nothing is ever recovered from a crashed agent.

## Upgrading

Run the new version against the same database. `initialize()` creates any table
that does not exist and adds any column that does not exist, on both backends:
the list of added columns is declared once in `store.py` and applied by whichever
store is in use, so an upgrade is not a thing you do by hand.

Two migrations are SQLite-only by design. They rebuild tables to repair foreign
keys and CHECK constraints that only ever went wrong in SQLite, because it
cannot alter a constraint in place; a PostgreSQL database created from the
current schema never had the defect they undo.
