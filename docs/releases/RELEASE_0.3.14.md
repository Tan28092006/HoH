# HoH 0.3.14 — commit-bound Planner reconciliation

Every observed Git commit is now a planning boundary. Remote fast-forwards are
expanded into ordered commit records; changes on the local default branch and
all fetched remote heads open a durable `repository_reconciliation` record.

While any observation is pending, the runtime stops new work offers, READY
transitions, execution claims, Developer candidate submission and integration.
Ticket context and inbox responses expose the barrier. Existing worktrees,
leases and frozen candidates are preserved until Planner decides their fate.

Planner must classify every ticket that was open at observation time as keep,
revise, cancel, supersede, complete or rework. It may stop an active stale lease
without deleting its worktree/artifact, retire frozen work when tied to the
pending reconciliation, record a Git-proven external landing, and create bounded
fix/adoption tickets. `planner.resolve_repository_reconciliation` checks the
decision manifest against actual ticket state before reopening execution.

This extends the paper's commit-to-next-Planner feedback loop to concurrent
Codex/Claude sessions and teammate branches. Historical tickets are never
physically deleted; terminal state and provenance remain auditable.

The user requested direct validation on the live Legal project. No automated
test suite is claimed for this release. Source compilation, package construction,
upgrade health and live workflow behavior are separate checks and must be
reported as such.
