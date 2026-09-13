# Harness-of-Harness paper alignment

This audit compares the control plane with Section 3 (pages 4-9), the Section 5
case study, and Appendix A (pages 21-29) of `2609.01481v1.pdf`. It distinguishes
method invariants from experimental controls that are specific to the paper's
single-machine benchmark setup.

## Method invariants

| Paper mechanism | Control-plane implementation | Status |
| --- | --- | --- |
| Carry artifact `A_t` and evidence `E_t` across iterations | Immutable Git candidates/mainline lineage carry the artifact. Candidate-bound findings, public test records and evidence bundles carry verified and gap records. | Enforced |
| Keep `D_t` within one iteration | One ticket packet is the current development document. Historical tickets remain auditable, but their full development documents are not injected into another ticket. New work inherits bounded verified behavior and live feature decisions by typed reference. | Enforced |
| Planner derives one bounded, locally complete observable increment from public specification, prior evidence and read-only artifact inspection | Planner role cannot implement. Ticket objective, criteria, preservation constraints, dependencies, scope and validation contract materialize the increment. The role skill limits a planning increment to at most three related priorities. | Contracted; semantic boundedness still requires Planner judgment |
| Developer is the single writer and warm-starts from the current artifact | Exclusive leases, write-scope collision checks, ticket worktrees, recorded base/SHA and clean candidate submission preserve lineage. Planner and QA have no candidate-write tools. | Enforced |
| Developer uses baseline -> change -> focused retest; its claims do not certify acceptance | Developer skill requires baseline and shift-left checks. Claims and public runtime records flow to QA as advisory input only. | Enforced |
| QA assesses a frozen, read-only candidate independently | QA lease is bound to candidate ID/SHA. QA cannot edit or submit a candidate. A changed SHA requires fresh QA. | Enforced |
| Derive scenario-specific black-box and white-box checks | Each acceptance criterion has exactly one scenario, method, expected observation, evidence types and capabilities. Extra risk-based checks remain allowed. | Enforced minimum contract |
| Claim -> observe -> assess; verified and gap sets are disjoint | Verdict validation requires every contracted criterion and evidence type. Findings use verified, failed, flaky or insufficient-evidence status and materialize `verified_records` and `gap_records`. | Enforced |
| Preserve public execution records, runtime identity and traces | The runner records command digest, actual child manifest, resolved source, candidate commit, exit/timeout/duration, capture/termination status, environment digest, storage reference and checksum. Large logs/media stay in evidence storage. | Enforced for the registered runner; exploratory domain tools attach records through evidence references |
| Equip QA with project/build/runtime/domain/visual/task-specific tools | The project verification profile declares intended equipment. Every QA session registers what it checked, while Planner/operator independently approves the subset backed by public preflight evidence. Strict tickets are neither offered nor claimable unless required capabilities are both declared and approved. UI contracts can require browser trace, screenshot and interaction capabilities. | Enforced routing, approval and claim gates; adapters remain project-specific |
| Return compact evidence to the next Planner | QA handoff carries preservation constraints, update targets and validation requirements. A successful gated merge now creates exactly one durable `delivery_completed` Planner report; recovery is idempotent. | Enforced |
| Re-plan from the real artifact at every iteration boundary | Every newly observed commit opens a durable repository reconciliation. Planner reviews each commit and classifies every ticket that was open at observation time before READY, claim, Developer submission or integration can continue. | Enforced by runtime barrier; semantic dispositions remain Planner judgement |
| Emit one artifact commit per Developer iteration | Candidate Git verification accepts only one ordinary commit whose sole parent is the ticket's frozen base; multi-commit ranges and merge commits fail before QA. | Enforced |
| Keep evaluator-only information outside the development loop | Role contracts forbid hidden tests, scores, private rubrics/formulas and evaluator rationale in tickets or evidence. | Contracted; a general-purpose service cannot automatically classify arbitrary pasted text |

## Multi-agent extensions

These mechanisms are absent from the paper because its roles run serially on one
machine. They preserve the paper's boundaries when several Codex/Claude sessions
and machines work concurrently:

- timed leases, capacity-aware durable queues and idempotent jobs;
- one mutable worktree per active Developer ticket and immutable candidate SHAs;
- an Integrator role for protected, candidate-bound mainline changes;
- agent-workspace mode for a separate clone on another machine;
- typed snapshots and stale-context rejection before consequential writes;
- authenticated shared HTTP and event waits for GUI sessions;
- repository observations for every remote branch without pulling or changing
  the shared integration checkout.

The Planner fetches all remote branch heads at session start, before READY, and
after a delivery report. Detailed branch diffs enter a ticket only when that
branch is linked to the active ticket/candidate or is a declared shared team
branch. This lets the board notice requirement/docs/API/config/production changes
without making every unrelated branch invalidate every ticket snapshot.

A changed head may reveal several commits after an offline interval. HoH records
each fast-forward commit in order inside one bounded observation, then requires a
decision for every ticket that was open at that boundary. Old tickets are retained
for audit but leave dispatch as `CANCELLED` or `SUPERSEDED`. Active stale execution
can be revoked without deleting its worktree or frozen candidate. Code seen only
on a peer branch becomes an adoption/QA/integration path; code already on the
default branch becomes DONE only through the explicit Git-verified ungated-landing
record, which carries no QA certification.

## Context boundary

The normal packet budget is 4,000 estimated tokens. Identity fields repeated by
nested rows are removed, long narrative entries become 200-character checkpoints
with resolvable references, and old progress narration is removed before typed
decisions. The latest copy of each decision type is retained. The former
`latest_planner_decision` duplicate was removed because the same typed entry was
already present in `context_entries`. The visible repository state is stored once;
the stale-write snapshot retains only its digest.

Full history remains durable in the database and evidence store. It is fetched by
typed reference only when needed; it is not copied into every role prompt. Verified
behavior inherited from earlier tickets is capped and carries its source ticket.

## Deliberate differences and remaining limits

- The paper fixes one harness-model configuration to control an experiment. This
  deployment intentionally permits Claude and Codex to fill different roles. It
  fixes role contracts, runtime authority and evidence schemas instead of forcing
  the same model across the team.
- The paper uses three serial role invocations and a fixed iteration budget. This
  system adds Integrator and allows several disjoint increments concurrently.
  Artifact scope and candidate identity, rather than global serial execution,
  prevent mixed evidence.
- A new push to a remote branch is observed at a Planner sync boundary. There is
  no Git-provider webhook in the local release, so an idle Planner is not woken at
  the instant an external push occurs.
- Browser, engine, hardware and domain adapters must still be installed on the QA
  machine. Capability declaration plus evidence-backed approval prevents unsafe
  assignment; HoH does not install or remotely attest proprietary equipment.
- General production readiness also depends on backup/restore rehearsal, external
  monitoring, multi-host artifact transport and PostgreSQL HA/load validation.
  Those operational controls are separate from the paper's development method.

The implementation is therefore aligned with the paper's quality loop after the
multi-agent adaptations above, but it is not an identical reproduction of the
paper's experimental apparatus.
