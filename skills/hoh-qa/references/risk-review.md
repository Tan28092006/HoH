# Follow the frozen review plan

Load only the `practice_skills` named in the worker brief. HoH keeps ticket,
lease, context and verdict authority; practice skills do not grant those rights.

- Low: one independent owner, focused regressions and every contract scenario.
- Medium: one independent owner reviewing the bounded batch. Use one authoritative
  full-suite result on the exact candidate with its frozen command and environment.
  Do not repeat it for each finding or advisory reader. Retain `adversarial_review`.
- High: two distinct authenticated owners, each responsible for every criterion.
  High-risk approval and adversarial evidence remain required before merge.
- Historical: keep the candidate's recorded owner count. Do not invent a second
  historical verdict or retrofit a new plan.

The registered release command and its SHA/command/configuration checks still
apply at integration. A focused low-risk review does not waive that release gate.

## High-risk advisory reviews

Use the existing `testing`, `ponytail-review`,
`thermo-nuclear-code-quality-review` and `pstack-interrogate` definitions.
For this invocation override fanout defaults with exactly three independent,
read-only leaf reviewers per owner, no recursive delegation and no full suites
in leaves. Inherit an available model if needed; do not install or switch billing.
Missing native spawning is an infrastructure blocker, not permission to omit leaves.

1. Read `qa.get_review_batch`. Resume its recorded native tasks after interruption.
2. If absent, call `qa.prepare_review_batch` with literal changed paths, at most
   eight files per leaf including at most three related files, and an A/B/C question.
   Different owner slots receive different assigned review lenses.
3. Give leaves only their returned scoped briefs and pinned artifacts, never the
   owner packet or lease instructions. Leaves cannot claim, heartbeat or submit.
4. Retain each actual native task ID and report using `qa.checkpoint_review_task`.
   Failed or missing leaves are blockers. Do not replace a terminal report or
   restart the fanout after a stale callback; read and resume the existing batch.
5. Wait for all completed reports, verify their evidence, synthesize findings,
   and refresh ticket context immediately before `qa.submit_verdict`.

The server enforces owner slots and completed checkpoint counts. A checkpoint
records the owner's report of native execution; it does not independently prove
that a model ran. Evidence remains candidate/review/author-bound and auditable.
