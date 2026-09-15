# Reconciliation and repository truth

`planner.sync_repository` is the only freshness probe for planning boundaries.
It fetches every configured upstream ref and reports local/upstream SHAs,
ahead/behind, incoming commits, changed paths, document paths and changed branch
refs without changing the checked-out branch. Review detailed branch diff only
when the branch is linked to an active ticket/candidate or is a declared team
integration branch. A remote commit or production note is provenance, not QA
evidence.

For every observation, use `planner.list_repository_reconciliations`, resolve
the typed observation and audit every ticket in `ticket_versions`. Give each one
evidenced disposition:

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
