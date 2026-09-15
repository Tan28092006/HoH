# Submission recovery and Planner intervention

If submit is interrupted or returns no submission ID, record its exact code and
message, then reread ticket/candidate/submission state before retrying. The first
attempt may already be queued. For `context_stale`, compare current ticket
version, lease owner, worktree/branch/base, candidate SHA, contract, dependencies
and repository state. Retry once with fresh snapshot and same idempotency key only
when no candidate/job exists and no material input changed. Otherwise resume the
existing job or route the changed fact to Planner.

Use `dev.report_blocker` only when no safe next code action remains while lease
is live. Its one durable note states class, observed fact/error, attempted action,
one decision needed, artifact/base, refs and safe next owner. Use
`work.report_failure` for failed provisioning/submission or another durable tool
failure with no returned Planner job. Do not use both paths for one fact.

Never report empty inbox, stale context, a lease/state conflict, a busy dependency
or first transient error. Do not weaken assertions, skip checks or alter contract
to make a run green.
