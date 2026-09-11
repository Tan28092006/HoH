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

The MCP server currently serves **stdio only**, and builds its identity from the
`HOH_AGENT_ID`, `HOH_AGENT_ROLE` and `HOH_PROJECT_IDS` environment variables.

Over stdio that is sound: the operator owns the process that sets those
variables. **It is not authentication.** Exposed on a network as-is, any caller
that reaches the port declares its own role, so a client could present itself as
`integrator` and move the protected default branch. Do not open a port to the
MCP server before HOH-243.

The REST/UI API is different: it already authenticates bearer tokens with
`hmac.compare_digest` against a token-to-actor map, so the operator console can
be served over a network today.

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
HOH-243  authenticated network transport for MCP      → unlocks remote callers
HOH-245  candidates from an agent-held workspace      → unlocks remote Developers
HOH-204  PostgreSQL store contract                    → unlocks multiple hosts
```

Until HOH-243 lands, keep the MCP server on stdio and reachable only from the
machine it runs on.
