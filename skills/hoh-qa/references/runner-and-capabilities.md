# Runner, provenance and capability recovery

The verification profile is intended equipment, not permission. QA declares only
the browser/runtime/build/source-inspection/domain adapters it actually checked
on this machine, then Planner independently approves public preflight evidence
for the same `machine_id`. A missing capability never reduces the contract.

Before relying on results, verify HEAD, source/import paths, command and
environment identify the frozen candidate. Use the runner's provenance status,
actual-child manifest and termination/capture fields. A standalone Python import
does not prove pytest provenance. Candidate modules must resolve inside its
worktree; a browser must serve its build. Use isolated data/processes, never the
live control-plane database for destructive or concurrency checks.

An infrastructure terminal result can park the unchanged candidate as `BLOCKED`
and release the QA lease. Read its evidence, communicate the precise gap and stop
verdict writes. Do not repair HoH or modify frozen command/candidate. After real
remediation, Planner records readiness evidence and recovers the same candidate.

Record `insufficient_evidence` when a required check cannot run. A durable QA
tool/worker failure without an existing Planner job gets one `work.report_failure`
with run/evidence references. QA verdicts and recognized runner outcomes already
create Planner feedback; do not duplicate every failed scenario.

For each finding name the criterion, accurate status (`verified`, `failed`,
`flaky`, `insufficient_evidence`), reproducible evidence, observation and user
impact. Continue safe independent checks after an early failure but mark blocked
criteria unverified. Evidence is immutable to candidate SHA and environment digest.
