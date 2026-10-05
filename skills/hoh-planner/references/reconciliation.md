# Reconciliation and repository truth

`planner.sync_repository` is the only freshness probe for planning boundaries.
It fetches every configured upstream ref and reports local/upstream SHAs,
ahead/behind, incoming commits, changed paths, document paths and changed branch
refs without changing the checked-out branch. Review detailed branch diff only
when the branch is linked to an active ticket/candidate or is a declared team
integration branch. A remote commit or production note is provenance, not QA
evidence.

For every observation, use `planner.list_repository_reconciliations`, resolve
the typed observation and audit every ticket in `ticket_versions`. A
`scope: tickets` reconciliation (QA rejection, gate regression, drift onto an
already-observed main) captures only `frozen_ticket_ids`, the judged ticket and
its dependents; the rest of the board keeps executing meanwhile. Give each
captured ticket one evidenced disposition:

- `keep`: the changed artifact cannot affect requirement, scope, dependency,
  candidate or contract; write why.
- `revise`: update plan, dependency, scope or validation contract from current
  code/spec.
- `cancel`: the work has no remaining observable purpose.
- `supersede`: another bounded ticket carries it; name that replacement.
- `complete`: only controlled merge or Git-verified external landing satisfies
  the behavior.
- `rework`: current code or evidence contradicts planned behavior.

Create bounded fix, adoption or regression tickets for new gaps and include
their IDs in the decision. Call `planner.resolve_repository_reconciliation` only
after every captured ticket is covered; never resolve merely to unblock queue.

For obsolete nonterminal work use `planner.retire_ticket_for_reconciliation`
with `CANCELLED` or `SUPERSEDED`. It revokes non-merge execution and preserves
the worktree/candidate/evidence. Use `planner.stop_ticket_for_reconciliation`
only when work remains valid but must pause for revision. A running merge becomes
pending retirement until HoH checks main: `APPLIED` means retired; `LANDED` means
the code reached default branch and Planner must assess a bounded revert rather
than call it cancelled.

If external code already implements a ticket on a non-default branch, stop
duplicate development and make a bounded adoption/review path that keeps the
branch/SHA and still passes QA/integration. If it is on default branch, use
`planner.record_external_landing` after Git verification. Neither path fabricates
QA evidence or transfers evidence to a changed SHA.

HoH also observes Git while no Planner is open: a new commit still opens its
reconciliation. A ticket whose candidate is already on the default branch (or
whose diff landed as a cherry-pick) is recorded for you as an ungated landing by
`system:sweep`: classify it `complete`. A job whose summary starts
`candidate_already_on_main` means that record was refused: record the landing
as above, or decide a revert or rework. One starting `merge_job_stuck` names a merge job that
outlived the worker timeout and that recovery cannot settle. Close it with
`planner.close_stuck_merge_job` (recovery runs once more; the ticket is
untouched), then record the landing or retire the ticket as its Git facts say.

After every landing HoH checks `main` with the project test command. A job
starting `merge_queue_paused` means no merge starts in the project: a red main
(a revert ticket names the commit; complete its contract and ready it), too
many consecutive failures, or the hourly merge budget (clears itself). Decide,
then `planner.resume_merge_queue`; set limits with
`planner.configure_run_guardrails`.
