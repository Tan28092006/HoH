# Bounded ticket design

Plan from the current public specification, current artifact and candidate-bound
QA evidence, never from a copied historical development document. Every
executable ticket needs one observable objective, independently verifiable
acceptance criteria, relevant preservation constraints, required role,
dependencies and repository-relative write scope when code changes.

Split by coherent observable behavior rather than file count. Avoid prescribing
implementation unless it is an established project constraint. Prioritize build
blockers, regressions and insufficient evidence ahead of feature expansion.
Schedule disjoint scopes when leases/runtime permit; sequence overlaps and real
dependencies. At milestones add bounded end-to-end journey coverage for gaps
between individually passing tickets.

Update write scope only for `BACKLOG`/`BLOCKED` tickets using current repository
paths and expected version. Never alter an active execution ticket's scope.
Requirements, public API/schema/config and production/runtime changes can revise
contracts; source-only changes may still affect preservation or candidates.
