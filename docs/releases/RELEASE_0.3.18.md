# HoH 0.3.18 — observable QA dispatch and bounded context

QA discovery now follows ticket lifecycle state. A Developer-owned ticket stays
visible to QA after its frozen candidate reaches `QA_READY`, and Integrator
discovery likewise follows `VERIFIED`/`MERGE_READY` instead of filtering on the
ticket's originating `required_role`.

Strict capability dispatch remains fail-closed. When a queued candidate cannot
be offered, `work.inbox` returns bounded `capability_blockers` and QA discovery
returns a `capability_state` with the missing declaration, missing independent
approval and machine-binding result. QA receives one fingerprinted durable
`capability_blocked` event instead of silently treating the queue as empty.

Planner now receives `capability_review_required` for incomplete declarations as
well as complete but unapproved declarations. The payload separates equipment
that QA must verify and re-register from capabilities that Planner may approve
only after public preflight evidence on the same machine. Skills and generated
role prompts require both roles to process those fields before idle wait.
If another online QA already has a matching approved profile, the blocked
identity and Planner are not woken; normal dispatch routes the candidate to the
capable peer.

Ticket context now applies field and entry-type projection by role before budget
fitting. It removes timestamps, repeated acceptance criteria and metadata the
reader cannot act on, and keeps only current typed decision checkpoints in the
normal packet. Raising `token_budget` no longer silently fills the response with
old history. Planner/Admin can deliberately set `include_history=true`; execution
roles cannot widen their field visibility. Feature context always returns its
thin rollup rather than expanding ticket bodies merely because they fit.

Experience transfer is now bounded to three reinforced, file-scope-overlapping
advisory checkpoints. Same-ticket lessons reach replacement sessions, long
lessons carry an `experience:<id>` reference for one-item resolution, and only
Planner/Admin can enumerate the project-wide notebook. Skills reject progress or
ticket-state narration as experience and require Planner to recheck a lesson
against current code/spec before planning from it.

Per the live-pilot validation policy, this release uses source compilation,
package construction, service health and database-preservation checks. It does
not claim an automated suite run.
