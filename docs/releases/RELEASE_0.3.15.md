# HoH 0.3.15 — one commit per Developer iteration

Candidate verification now requires the submitted candidate to be exactly one
ordinary commit whose sole parent is the frozen ticket base. Multi-commit ranges
and merge commits are rejected before a candidate can become QA-ready.

This closes the remaining gap between the documented Developer prompt and the
runtime: each accepted Developer run produces one immutable commit, QA evaluates
that artifact, integration lands it, and the resulting repository observation
wakes Planner for the next reconciliation cycle.

The user requested direct validation on the live Legal project. No automated
test suite is claimed for this release. Source compilation, package construction,
upgrade health and live workflow behavior are separate checks.
