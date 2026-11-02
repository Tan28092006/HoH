# Risk-directed QA policy

This is the operator-authorized source policy from 2026-11-02, not a statement
that it is installed or that an external audit is a HoH verdict.

## Frozen candidate plan

At both synchronous and queued submission, the server computes risk from Git
changed paths, falling back to scope only when paths are unavailable. It stores
`candidates.qa_review_plan` and `required_qa_reviews` once. Later project policy
edits cannot lower that candidate's owner count, risk gates or advisory budget.

| Risk | Independent owners | Mandatory advisory leaves per owner | Review execution |
| --- | ---: | ---: | --- |
| Low | 1 | 0 | Focused regressions and contract scenarios |
| Medium | 1 | 0 | Bounded batch with one shared exact-candidate full-suite result |
| High | 2 | 3 | Specialized bounded advisory reviews and independent synthesis |

Risk follows impact, not diff size. Known authorization, credentials, data,
migration, concurrency and policy paths have a mandatory high-risk floor for
new candidates. Projects may add sensitive patterns, not lower that floor by
emptying their configured list. File count can raise low to medium, never lower
a sensitive one-file change. Before a candidate exists, scope risk is provisional.

Existing candidates retain their recorded review counts and evidence. Pre-dual-QA
rows migrate with one owner; existing dual-QA rows retain two. A null plan is not
backfilled or relabelled as a new policy decision. Live historical QA leases are
bound to review attempts without inventing verdicts.

## Platform enforcement

`qa_reviews` binds each attempt to candidate, slot, reviewer and lease. A maker
cannot claim, judge or merge its work. Required owners must be distinct
authenticated identities. Model family is optional provenance, as in installed
0.3.35, not authorization. Role, project scope, stable-machine preflight approval,
capacity, lease fencing and fresh packet checks remain.

High-risk owners prepare three scoped A/B/C briefs and record distinct actual
native task IDs and completed report references. Scopes and terminal reports
cannot be rewritten. The server refuses a verdict until every required advisory
checkpoint is complete. It records the owner's report of execution, not proof
that a native model ran. Batches are private to their owner/review and are absent
from general context packets and history.

Each owner must independently cover every criterion and any required
`intent_check`. One pass leaves a dual-owner candidate pending. Settlement emits
one aggregate outcome after the required owners return. An interrupted reviewer
does not revoke its peer; a failed review plus an expired peer settles rejection.
Readiness recovery replaces review slots explicitly while retaining old feedback.

## Reuse without weakening evidence

Owners reuse one active or current passing suite job for the same candidate,
frozen command digest and timeout. Polling renews only the caller's own lease.
Reuse additionally requires the trusted runner's versioned execution-environment
digest at queueing, execution and both reuse consumers. Distribution inventories
do not prove actual consumption. General module commands, dynamic imports,
unpackaged/omitted dependencies and editable packages remain UNKNOWN. No package
name, nonempty RECORD or nominal interpreter probe confers reusable authority.

Execution removes inherited PYTHON controls. The narrow certifiable boundary is
literal inline pass/assert/print code with no imports or dynamic expressions,
restricted to at most 1,024 characters of unescaped ASCII source, with integers
at most 64 bits and no comments/directives. A host AST
constant alone does not prove the selected compiler's dependencies; other forms
remain UNKNOWN and execute ordinarily. Proved forms use the runner's enforced
`-I -S -B` startup. The selected CPython runtime capture uses
that same isolated/no-site startup and hashes the actual runtime image and loaded
startup software, including source/cache bytes. If that proof is unavailable,
the ordinary declared invocation executes with UNKNOWN applicability. Other
flags/commands remain authorized to execute
normally, subject to existing provenance guards, but are not certified for reuse.
There is no editable-package name exemption or blanket `-m` certification.
Command, timeout, provenance modules and OMP/OpenBLAS controls remain bound.

The digest excludes checkout paths, free memory, reviewer identity and secret
environment values. Old partial or unknown attestations are not backfilled with
today's fingerprint. Unknown or changed applicability cannot reuse a passing run;
an active unproved/mismatched join is refused without creating another job.
Observation or continuation of one authorized UNKNOWN run is not reusable
certification and does not alone revoke its independent observers.
External capture runs outside DB transactions; publication/authority checks
re-read the current profile, candidate, lease/review, retirement and latest attempt
under short transactions. A no-test-slot merge retains that capability through
the locked consumer: a late applicability miss removes its owned intent and
defers to QUEUED for proper re-admission, never a suite outside its slot.
Python dispatch selects the interpreter's command mode before program argv;
grouped selectors retain their effective prefix flags and pytest observation.
Trailing `-m pytest` after `-c` or a script does not select instrumentation, and
option operands are not selectors. The observer restores implicit cwd only for
ordinary module startup, not safe-path (`-P`) or isolated (`-I`) startup. A fresh
pytest result still needs completed counts and frozen source provenance; UNKNOWN
cache applicability never permits an all-skipped run to pass.
Observed failures, collection errors or a recorded non-success pytest status
also preclude a fresh pass, even when a project hook overrides process exit to
zero and other tests passed. Existing infrastructure/provenance classifications
remain distinct. The observer's skippable first line preserves accepted
interpreter `-x` prefixes during module-to-script adaptation; pytest's trailing
`-x` argument remains unchanged.
Recorded non-success pytest status takes precedence over a project hook's process
exit normalization for pytest failure attribution. Existing memory-marker
recognition also uses that non-success outcome, preserving collection-resource
infrastructure attribution and its bounded retry. Successful printed markers
alone do not establish resource failure; ordinary candidate collection errors remain
candidate failures.

The deadline/stop controller performs only fresh DB state/profile/lease/review/
candidate/retirement checks. It does not call external capture. The existing
managed worker performs full capture/binding checks before execution and result
publication, outside writer transactions. Monitoring is not an environment or
reuse certificate: unavailable or changed known bindings still cannot be used
by the worker's full checks. No background observer/cache or extra executor is
needed, and blocked capture cannot occupy the managed cancellation/deadline loop.
The immutable requesting lease remains execution provenance; an active peer or
the completed required passes can sustain the shared execution after it ends.
A newer pending or failed attempt fences an older green result. Source,
checksum, command and frozen configuration binding remain required.
When a shared job exists, the final passing owner keeps its lease until that
attempt has a usable outcome; it cannot publish a pass ahead of a late red run.

The low-risk QA budget does not waive the registered project release command or
integration's existing regression gate. Projects must configure a valid command;
reviewers must not silently substitute a smaller command to claim a full pass.
Medium/high share the authoritative full run for the batch, not conclusions or
approval from another SHA. New-plan or historical dual-owner drift returns to
Developer rather than carrying approvals onto a server-derived artifact.

Medium/high still need `adversarial_review` evidence. High also needs
`policy.approve_high_risk` on the exact candidate. Merge checks the current owner
passes and current regression identity, including after a queued gate runs.
External landings remain ungated, never fabricated QA approval.

Adversarial evidence must be referenced by a verified finding from a current
passing review. Failed, expired, replaced or unreferenced evidence remains
historical. Untouched legacy single-owner candidates without review attempts
use their latest verified unbound finding batch.

The legacy single-owner Git rebase mechanism remains. Every newly published
derived identity freezes its own impact-based plan and returns to QA_READY,
even for disjoint changes. It never inherits the old artifact's approvals or
evidence linkage. The original historical row stays unchanged.

Bootstrap and worker briefs advertise the budget. Role cards load existing
practice skills only as needed. They do not add another dispatcher, change global
practice definitions or grant advisory children lifecycle authority.
