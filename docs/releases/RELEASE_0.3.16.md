# HoH 0.3.16 — closed-loop commit iterations

The one-commit candidate gate now supports rework correctly: the next ordinary
commit may descend from the prior QA-assessed candidate, or restart from a newly
frozen base after mainline drift. It still rejects merge commits and hidden
multi-commit ranges.

QA rejection, integration regression/base drift and successful controlled merge
now open a durable Planner reconciliation in the same transaction that records
the result. The downstream queue remains durable but cannot offer or claim work
until Planner reads the candidate/test/merge reference, audits the captured open
tickets and resolves the decision manifest. Successful merges also advance the
locally observed mainline SHA so the next Git sync does not duplicate that
iteration boundary.

The user requested direct validation on the live Legal project. No automated
test suite is claimed for this release. Source compilation, package construction,
upgrade health and live workflow behavior are separate checks.
