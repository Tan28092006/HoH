# Deploying the control plane

This describes what can be deployed today, what each choice costs, and what is
still missing before a deployment can be called production. It is written
against the spec in `HOH_MULTI_AGENT_CONTROL_PLANE.md`; where the two disagree,
the spec states the requirement and this file states the current reality.

## Where a working tree lives

`planner.configure_repository` records a `workspace_model` per project. It
decides whether the service is allowed to hand an agent a filesystem path.

### `colocated` (default)

The agent process runs on the same machine as the service. `dev.prepare_worktree`
creates a worktree under the service's own data directory and returns its path,
which the agent can open because it is the same filesystem.

This is what a single-VPS deployment uses: the service and every agent process
run on that VPS, and a person drives them remotely. It needs no new worktree
code, and every guarantee the control plane already enforces — candidate
immutability, write-scope collision, lease ownership, Integrator-only movement
of the default branch — holds unchanged.

### `agent_workspace`

Each Developer holds its own checkout on its own machine. The service therefore
provisions nothing, and `dev.prepare_worktree` and `dev.queue_worktree_provision`
refuse with `workspace_model_not_colocated` rather than returning a path the
caller could not reach.

This model is **not yet able to accept candidates**. Candidate identity is
derived by running Git against the tree that produced the commit, and the
service does not hold that tree. Making it work needs a repository the service
can read for itself — the commit a remote Developer claims must be verified
against bytes the service fetched, never against the Developer's word. That is
HOH-245.

The model cannot be changed while the project has worktrees or active Developer
leases created under the current one, so a switch cannot strand work in flight.

### Which role needs which

| Role | Needs a working tree? | Can run remotely today |
|---|---|---|
| Planner | No | Yes |
| Developer | Yes | Only co-located with the service |
| QA | Yes — it executes the frozen candidate | Only co-located with the service |
| Integrator | No — it makes its own integration checkout | Yes, if it can reach the repository |

## Transport and identity

`HOH_MCP_TRANSPORT` selects the transport and defaults to `stdio`, so an
existing local install is unchanged and listening on a network is always an
explicit decision.

### stdio

Identity comes from `HOH_AGENT_ID`, `HOH_AGENT_ROLE` and `HOH_PROJECT_IDS`.
That is sound here and only here: the operator owns the process that sets those
variables. It is **not** authentication, which is why those variables grant
nothing on any other transport.

### `streamable-http` / `sse`

Requires `HOH_MCP_TOKENS_JSON`, a map of bearer token to identity:

```json
{
  "<a long random token>": {
    "agent_id": "developer-01",
    "role": "developer",
    "project_ids": ["hoh-control-plane"]
  }
}
```

The server refuses to start on a network transport without it, rather than
serving anonymously. A request is authenticated before it is dispatched, so an
unauthenticated caller never reaches a tool. Each credential gets its own server
instance bound to its own identity, so another role's tools are **absent** from
the surface a caller reaches rather than merely refused when invoked — a
developer credential cannot see `integrator.merge_candidate` at all.

Tokens are compared with `hmac.compare_digest`, must be at least 32 characters,
and never appear in audit events, activity or error text. `HOH_MCP_HOST`
defaults to `127.0.0.1`: put a TLS-terminating reverse proxy in front before
binding anything wider.

The REST/UI API authenticates separately, with its own bearer tokens, so the
operator console can be served over a network today.

## Storage, and what multi-machine actually requires

SQLite is the current store. The spec restricts it to a local pilot and forbids
putting it on a network share — but the boundary is narrower than "one machine":

- **One host, many processes: supported.** The service, its detached workers
  (QA runner, integration, worktree provisioning, candidate submission) and
  co-located agents all open the same database file. HOH-214 bounded the
  contention this creates: write transactions hold only short mutations, Git and
  test execution run outside them, and lock contention surfaces as one documented
  retryable error rather than a raw SQLite `locked`.
- **Many hosts: not supported, and not a configuration change.** SQLite's locking
  assumes a local filesystem. Over NFS/SMB its advisory locks are unreliable, so
  two hosts can both believe they hold the write lock and corrupt the file. This
  is why the spec forbids the network share outright rather than discouraging it.

So the ladder is:

1. **Single VPS, SQLite, co-located agents** — works with today's code plus
   HOH-243 for an authenticated transport. This is a real deployment, not a toy.
2. **Multiple hosts** — needs the store contract moved to PostgreSQL (HOH-204).
   The invariants that must survive the move are the ones the lease and queue
   tests already assert: a claim is exclusive under concurrency, a queue offer is
   handed out once, optimistic `expected_version` checks reject a stale write,
   and idempotency records replay rather than duplicate.

PostgreSQL is the right target rather than a second SQLite trick because those
invariants are all transactional: exclusive claim is `SELECT ... FOR UPDATE` or a
unique partial index, queue hand-off is `FOR UPDATE SKIP LOCKED`, and the
existing optimistic-version pattern carries over unchanged. Nothing in the
current schema depends on a SQLite-only feature.

## Order of work

```
HOH-244  workspace model, decided and enforced        (done)
HOH-243  authenticated network transport for MCP      (done)
HOH-245  candidates from an agent-held workspace      → unlocks remote Developers
HOH-204  PostgreSQL store contract                    → unlocks multiple hosts
```

A single VPS running the service, its workers and co-located agents, reached
over an authenticated transport, is deployable today. Remote Developers wait on
HOH-245; more than one host waits on HOH-204.
