# Firstmate as the HoH runner

How one Claude Code or Codex session running the `/firstmate` skill dispatches
HoH work. There is no crew process and nothing launches headless agent CLIs:
the coordinator is an ordinary chat, and every ticket is worked by a fresh
native subagent. This is the pattern used in the HOH-280 live run:
claim, `worker_brief`, fresh subagent, `returns` only.

## Before you start

- The HoH host must be running (`deploy/desktop/hoh_desktop.py --host` or the
  installed service). If a role MCP answers ECONNREFUSED, stop and say so; do
  not reconstruct ticket state from files or old chat.
- Connect every role MCP profile for the project in this one session: Planner,
  Developer, QA and Integrator (one credential each, from `local_agent` or the
  agent kit). Each profile exposes only its role's tools.
- Call `session.bootstrap` and `agent.register` once per profile. `agent.register`
  starts the session that `DISPATCHER_TICKET_LIMIT` counts.
- Keep the firstmate run record under `.firstmate-native/runs/<run-id>/run.md`
  (see the skill's `references/state.md`). It is a checkpoint, not HoH state:
  HoH's database stays the only ticket truth.

## Planner work

Planner work (repository sync, reconciliations, Planner jobs, ticket authoring)
runs in its own subagent with the Planner profile and a bounded brief: the
project, the job or reconciliation id, and the decision expected back. The
coordinator does not reason about reconciliations itself; it waits for the
subagent's decision and records it in the run record.

## The dispatch loop

1. `work.discover` (or `work.wait_for_event` while idle) with the role profile
   that has an offer. Keep the query narrow; do not dump the board.
2. Claim through that role's MCP: `work.claim` for Developer,
   `qa.claim_candidate` for QA, `integrator.claim_candidate` for Integrator.
3. The claim result carries `worker_brief`. Pass it **verbatim** to a fresh
   native subagent (Claude Code `Agent`, Codex native subagent). Do not
   summarize it, add ticket history, or reuse a worker from another ticket.
   Add only: "return only the fields listed in `returns`; do not start another
   supervisor; do not edit the run record".
4. The worker uses the role MCP tools the brief names, under the lease and
   snapshot the brief carries. The server, not the coordinator, enforces lease,
   role and context freshness.
5. The worker returns only the brief's `returns` fields (for example
   `submission_id, commit_sha, status, blockers` for a Developer). Treat them
   as evidence: confirm the resulting state with one bounded `work.discover`
   or ticket read before recording the ticket as ready or done.
6. Call `work.report_usage(ticket_id, worker="fresh_subagent", tokens_in?,
   tokens_out?)` for that round, then update the run record (task id = ticket
   id, worker id, status, returned fields, next action).
7. On an unhandled failure, report it once with `work.report_failure` before
   going idle.

## Independence

- QA runs on a different model family from the Developer that wrote the
  candidate (for example Developer on Claude, QA on a Codex/GPT model, or the
  reverse). Pass `model` on the Agent call where the host allows it; otherwise
  run QA from a session on the other client.
- A worker never edits a submitted candidate and never patches HoH.

## Concurrency

- Bound in-flight workers by host memory, not by the number of offers. The
  host runs one project suite at a time (the test slot), and a QA or gate run
  of the HoH suite needs several GB; on a 16 GB machine keep at most two or
  three workers in flight and never configure `-n auto` in a test command.
- Wave 6G: after `DISPATCHER_TICKET_LIMIT` (5) distinct claimed tickets in one
  session, an idle `work.wait_for_event` returns `advice: start_new_session`.
  Follow it: stow the run record and resume in a new session.

## Known limits

- **Self-approval classifier.** Claude Code may refuse to let one session
  merge a candidate it also wrote. Either add a permission rule for the
  Integrator tools up front, or run the Integrator profile from a separate
  session.
- **HoH must be running.** Firstmate supervises nothing after the chat
  closes; a closed coordinator leaves leases to expire and offers to return.
- Native subagents may not survive compaction or a session end; on `resume`,
  reconcile the run record against HoH before claiming again.
