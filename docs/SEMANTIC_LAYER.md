# Putting the control plane in git

The idea: the control-plane database lives in the repository, so a clone carries
not just the code but the reasoning that produced it — tickets, decisions,
evidence, verdicts. The repo becomes self-describing, and `git log` answers *why*
this code exists rather than only *what* changed.

This is worth building. But "put the database in git" is the wrong shape of the
idea, and the measurements say why.

## What is actually in the database

Counted on the live control plane, 5,024 rows across 31 tables:

| | rows | share | tables |
|---|---|---|---|
| Coordination | 3,266 | 65% | 14 |
| Semantic | 1,758 | 35% | 17 |

**Coordination** is `leases`, `work_queue`, `work_offers`, `dispatch_outbox`,
`agent_presence`, the four job tables, `idempotency_records`, `worktrees` and
`worktree_assignments`, and `agent_tool_calls`. It has three properties: it
changes constantly, it needs mutual exclusion, and **none of it means anything a
week later**. An expired lease is garbage. A claimed queue offer is garbage. A
worktree path is meaningless in anyone else's clone.

`agent_tool_calls` alone is 2,468 rows — 49% of the entire database — and it is
a redacted activity log, pure operations.

**Semantic** is `projects`, `features`, `tickets`, `ticket_dependencies`,
`candidates` and their changed paths and test profiles, `qa_findings`,
`evidence`, `qa_test_runs`, `context_entries`, `audit_events`, and the project
configuration. It is append-mostly, low-frequency, and permanently valuable.
This is the "why" layer, and it is only a third of the rows.

So the naive version would put 65% churn into the repository in exchange for
nothing, and the single largest contributor would be an operations log.

## Two reasons live state cannot be git

### Git has no mutual exclusion

The control plane's whole value is that exactly one Developer holds a ticket.
Git offers no locking primitive. Two agents claiming the same ticket produce two
commits, reconciled by a merge — which is last-writer-wins or a conflict, never
mutual exclusion. `leases` is one row in the whole database, and it is precisely
the row git cannot represent.

### The recursion is worse than it first looks

The database coordinates work **on the repository**. If ticket state lives in
that repository, every transition is a commit to the thing the tickets are
about. Follow it through and both branches are fatal:

- **Transitions commit to the default branch.** Then a Developer writes to the
  protected branch merely by claiming a ticket, which is the one thing
  Integrator-only movement exists to prevent.
- **Transitions commit to the agent's own branch.** Then no other agent can see
  the claim until it merges, so mutual exclusion is gone entirely — and the
  candidate now carries plan changes, so base drift on code becomes base drift
  on the plan.

Live transitions therefore cannot be commits. That is not a limitation to
engineer around; it is what the two systems are each for.

## The shape that does work

Split along the seam the domain already has, and let each side do what it is good
at:

**Coordination stays in the database.** Leases, queues, jobs, presence,
idempotency. It needs transactions and locking, and it is disposable.

> Status: only the removed synchronous merge path wrote this record. The queued
> merge worker does not write it yet; `semantic_record.py` and
> `GitWorktreeManager.commit_semantic_record` remain for that port.

**The semantic record is written into the repository at merge, by the
Integrator.** Not the live transitions — the settled outcome. When a candidate
fast-forwards the default branch, the same commit carries a record of what that
change was for: the ticket, its objective and acceptance criteria, the QA
findings and evidence references that satisfied them, the decisions that shaped
it, and the candidate's identity.

This resolves both objections at once:

- No concurrency problem. One writer, the Integrator, already the only role
  authorized to move the branch.
- No recursion problem. The commit that lands the code **is** the commit that
  lands its meaning. They cannot drift apart, because they are the same commit.

And it produces exactly what makes the idea attractive. `git log` over the
semantic files answers why a change exists. `git blame` on a source line reaches
the acceptance criterion it was written to satisfy. A clone is self-describing
with no database to deploy. A fork keeps the reasoning.

## What it costs

- The record is **write-once at merge**, so it cannot be edited to match a story
  told later. That is a feature, and it means a correction is a new record
  referencing the old one, exactly as a superseding decision already works.
- Work that never merges leaves no semantic record in the repo. Its history
  stays in the database, which is correct: an abandoned attempt is operational
  history, not repository meaning.
- The database remains the authority for anything in flight. The repository is
  the authority for anything settled. A reader must know which question it is
  asking.

## Where it attaches

`HOH-246` put a `Dialect` and a `StoreConnection` between the service layer and
its driver. That seam is where a second store plugs in, and it is why this is now
a bounded piece of work rather than a rewrite: the semantic writer is a consumer
of already-settled state at one well-defined moment, not a new storage engine
threaded through 283 call sites.

The unit of the record should be the ticket, not the table. A file per ticket
holding its settled objective, criteria, verdict and evidence references reads
and diffs like a document; a file per table would reproduce a database in a
format that is bad at being one.

## What this is not

It is not a replacement for `HOH-204`. Multiple hosts still need a shared
transactional store for the coordination half, and nothing here changes that.
The two are complementary: PostgreSQL for what is in flight, git for what has
settled.
