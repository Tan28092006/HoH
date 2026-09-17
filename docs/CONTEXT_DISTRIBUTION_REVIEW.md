# Context distribution under long-running operation

Reviewed at 0.3.20 against the live database (3 projects, 22 open tickets) and
the service code. The question: when one role agent works for hours or days,
does HoH still hand it less, and better, context than a single agent holding
the whole project in chat would carry?

## What the numbers say

A single agent doing Planner, Developer, QA and Integrator work in one chat
carries every ticket it touched, every test log and every decision, and its
context only grows. HoH replaces that with one bounded packet per read:

| Read | Estimated tokens |
| --- | ---: |
| Developer packet, QA_READY ticket with 5-scenario contract, 27 changed paths, 5 decisions | 3,603 |
| Same ticket, BACKLOG state, no history | 2,300–2,700 |
| Planner discovery, 13 features / 60 tickets | 5,232 |
| Execution-role discovery | 250–860 |
| Role card + common card | ~1,000 words |

The packet is stable in size because compaction is by type (newest checkpoint of
each decision type survives; narration goes first) and inherited constraints
are capped at ten. What is not bounded is the `ticket` section, which is
protected: a Planner-authored validation contract of 6 scenarios already costs
about 1,000 tokens before any history exists.

## Edge cases and their status

Status: **enforced** (server refuses or corrects it today), **fixed in 0.3.20**,
**proposed** (worth a ticket), **documented** (operator or Planner discipline).

### Stale memory inside one long chat

A Developer chat that has read the ticket at v3, v5 and v7 holds three
objectives. The model may act on the wrong one.
- Writes that carry `context_snapshot_id` are refused when planning decisions,
  repository state, barrier, retirement, ticket/feature version, candidate or
  dependencies moved since that read. **Enforced.**
- The snapshot is optional. A write without it bypasses the freshness check
  entirely; only `expected_version` remains. **Proposed:** require a snapshot
  on `dev.submit_candidate`, `qa.submit_verdict` and
  `integrator.merge_candidate`, or return `freshness: "unverified"` and audit
  it, so the card's "refresh before submit" becomes observable.

### Citations that nothing could resolve

Packets cited `ctx:entry:<id>`, `ctx:candidate:<id>:developer_claims` and
`ctx:finding:<id>` in `omitted_sections`, and the card told roles to "resolve
typed references one at a time", but no tool answered them. A Developer whose
key decision was checkpointed at 200 characters had no way to read the rest
except asking Planner. **Fixed in 0.3.20** (`context.resolve`, packet-visibility
rules apply).

### Identifier cost and copy errors

16-hex identifiers were 2–3% of packet characters but, tokenised at roughly two
characters per token, closer to 5–6% of tokens, and a model copying one back
mistyped it often enough to matter. **Fixed in 0.3.20** (prefix + counter).
Commit SHAs are Git identity and stay at 40 hex; they repeat 5–7 times per
packet. **Documented:** Planner prose should quote 12-character SHAs; typed
fields keep the full value.

### Discovery as a board dump

Planner discovery has no token budget; it is bounded only by `limit` (50
tickets) and paginated by cursor. On `hoh-control-plane` it was 6,726 tokens
per cycle. **Fixed in 0.3.20** to 5,232 by dropping duplicated references and
the constant recommended call. **Proposed:** a feature-rollup mode for Planner
(features with counts and one reference, no ticket rows) as the default, with
ticket rows on request per feature.

### Contract size drives the packet floor

Validation contract items may be 2,000 characters (criterion) plus 4,000
(expected observation). Because `ticket` is protected, an oversized contract
blows every packet for the ticket's whole life. **Proposed:** tighten bounds
(600/600) for new contracts and report `estimated_tokens` of the ticket section
back to the Planner at `configure_validation_contract`.

### Reconciliation barrier while Planner is away

Any observed commit opens a barrier that freezes offers, claims, submission and
merge until Planner audits every open ticket. This is correct, and it means a
closed Planner chat stalls the whole plant. **Enforced** by design.
**Proposed:** execution roles waiting in `work.wait_for_event` should receive a
`planning_barrier_pending` event with the barrier's age, and the dashboard
should show it, so nobody sits blind and the operator knows to resume Planner.

### Lease expiry with a live local process

An expired lease does not stop the process that held it. Takeover provisions a
new attempt directory, preserves the previous one, and a running merge blocks
takeover until reconciled. **Enforced.** **Documented:** revoke or stop the old
chat before a takeover.

### Same identity in two chats

A second chat with the same instance hits "This identity has a pending wait";
a wait resume must match the recorded `wait_id`, project and role.
**Enforced.** Wait ids are now short and are not secrets; the credential is.

### Shared remote across two installations

Agent ids are derived from project, role and instance without an installation
salt, so two HoH installations with the same profile names would name branches
identically. Sequential attempt numbers make that collision more likely than
random ones did. The server refuses to reuse an existing branch or worktree
path, so the failure is loud, not silent. **Enforced (fails safe).**
**Proposed:** salt new profile identities with the installation id.

### Failure reports during rework

The failure fingerprint hashes the summary text, which often contains a SHA or
attempt id, so the next attempt's identical failure gets a new fingerprint.
`planner_jobs_one_open_per_ticket` still guarantees one open Planner job per
ticket, so the cost is one extra context entry, not a second job. **Enforced.**

### Long QA runs and machine memory

`qa.run_project_tests` is asynchronous with a polled `test_run_id`, and the
host pins BLAS/OMP threads to one, which is what kept the Legal suite at
~191 MB after an earlier 3.38 GB failure. **Enforced.** Capability approval is
bound to `machine_id`; moving a QA profile to another machine correctly
re-blocks it. **Enforced.**

### Database restore and counters

Counters live in the database, so a restore from backup is self-consistent.
Merging rows from two databases is not a supported operation. Rows created
before 0.3.20 keep their long ids, and packets for old tickets show both shapes
until superseded. **Documented.**

### Test environment drift

`.venv-hoh` carries an editable install of `tmp/planner-merge-handoff`, so
`pytest` run from another checkout silently tests that old tree unless
`PYTHONPATH` points at the checkout under test. The 0.3.20 validation set
`PYTHONPATH` explicitly. One pre-existing failure
(`test_event_wait::test_planner_receives_new_intervention_job`, returns
`repository_sync_failed` on a repository without a remote) fails on `main`
unchanged. **Proposed:** reinstall the editable package from the integration
checkout and fix the test's repository fixture.

## Role capability over long tasks

- **Planner** holds the only unbounded loop; the three-priority cap, the
  per-event reference routing in its card and the bounded discovery are what
  keep it from becoming the single agent it replaces. The barrier proposal
  above is its main operational risk.
- **Developer** must land one ordinary commit per attempt. Long work is split
  by Planner ticket design, not by the Developer carrying state across
  attempts; the packet, worktree and recovery patch are the carried state.
- **QA** never holds project context beyond the frozen candidate, contract and
  evidence bundle; long runs are jobs, not chat.
- **Integrator** sees only what a merge needs. Its long operation is the gated
  merge job, polled by id.

Each role's context therefore stays near its packet size regardless of how long
the project runs, which a single agent cannot offer.
