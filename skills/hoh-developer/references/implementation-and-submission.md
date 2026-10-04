# Implementation and candidate submission

For a resumed `DEV_IN_PROGRESS` lease owned by this identity, do not claim again
or provision another worktree. Refresh packet, call `work.heartbeat` on the
existing lease, and
verify its path/branch/base/changed files. Dirty files can be valid unfinished
work; preserve them. Resume a recorded provision job if assignment is not ready.

Establish a baseline when useful. Implement only the ticket outcome and write
scope, using `validation_contract` to create reproducible public observations. Run
focused changed-behavior and adjacent-regression checks; expand for shared
contracts, migrations, concurrency or concrete failures. Do not run the full
frozen project command: QA runs it once on the frozen candidate and merge reuses
that run. Independent end-to-end acceptance belongs to QA.

Before `dev.submit_candidate`, refresh `ticket.get_context`: a pre-claim snapshot
is stale because claim advances ticket version. Confirm lease/worktree/base/SHA,
contract, dependencies, retirement and repository state. Submit a real one-commit
SHA once per attempt with concise criterion-to-change mapping, commands/results,
required runtime records/media manifests, risk and explicit untested paths. Use
`dev.append_progress` only for a material checkpoint that helps a replacement
agent; normal polling and queued submission are not Planner wakeups.

The commit parent must be frozen base, or prior QA/Planner-assessed candidate in
rework. Merge commits and multi-commit ranges are rejected. Poll the submission
job with `dev.get_candidate_submission` while `QUEUED`/`RUNNING`; polling renews
the lease. On terminal failure,
preserve worker error and SHA. A slow job is not a blocker.

Rework begins from exact failing evidence and current contract. Add a focused
regression. Runner/import-path failure is not proof of a product defect: preserve
the candidate and route infrastructure. Takeover uses the newly assigned branch;
port only relevant checkpoint changes and submit the changed SHA for fresh QA.
