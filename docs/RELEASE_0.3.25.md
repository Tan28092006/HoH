# HoH 0.3.25 — the QA capability approval cycle cannot deadlock

Delivers HOH-278.

## The cycle

QA needed a lease to run tests and record preflight evidence. A lease needed a
Planner-approved capability. Planner could approve only against independent
preflight evidence. No tool could produce that evidence before a lease, so on the
marketing project QA waited for Planner and Planner waited for QA.

The live database showed how general this was: no QA agent in any project had
ever been approved for a single capability. The approval call itself accepted any
string as evidence and checked nothing; the rule against self-report lived only
in the Planner card, and a Planner that obeyed it had nothing it was allowed to
accept.

## What changed

- **Lease-free, host-observed evidence.** `qa.run_capability_preflight` has the HoH
  host probe the equipment the QA agent declared, on the machine it registered
  from, and records `preflight:<id>` bound to that agent and a fingerprint of the
  machine. No claim or lease is involved. It is idempotent: unchanged inputs and
  outcomes return the same record. Only outcomes, reason codes and an output
  digest are kept; no output, path, environment value or machine name.
- **Built-in probes** for `source_inspection` and `git_worktree` (Git in the
  configured repository), `dependency_manifest` (a manifest at the repository
  root), `build_execution` (the configured test command's executable runs), and
  `browser_interaction` and `screenshot` (an installed Playwright browser build
  plus a Node or Python Playwright driver for the project).
- **Planner-configured preflights** for everything else:
  `planner.configure_capability_preflight` sets a command that must exit zero on
  the host. It lives in its own table, like the test command, not in the portable
  verification profile.
- **Approval enforced by the server.** `planner.configure_agent_capability_profile`
  now accepts only `preflight:` records for the same project, agent and machine,
  and only capabilities that are both declared and proven. Self-report, unknown,
  other-agent, other-machine and unproven evidence each fail closed with a reason
  code.
- **One job instead of two waits.** When a QA-ready candidate is blocked on
  approval alone, HoH opens exactly one Planner job for the ticket, naming owner
  and action: `owner=planner action=capability_approval` with the exact call, or
  `owner=infrastructure action=capability_preflight_gap` naming what the host
  could not prove and why. It is opened when the preflight runs, or from the QA
  inbox if the candidate became QA-ready later; the inbox path starts no process.
  Retrying, or polling, adds no job and wakes the Planner no further. Approving
  closes the job.
- **A QA agent on another machine** is reported unproven with
  `qa_machine_is_not_this_host` and routed to infrastructure, since the host can
  only speak for itself.
- The Planner's `capability_review_required` event now carries `evidence_refs`
  and `proven_capabilities`, and no longer carries the raw `machine_id`. The QA
  capability state no longer echoes it either.

Nothing changes a candidate, a ticket's status or a verdict: the ticket stays
QA-ready, and after approval QA claims it normally with its exclusive lease.

## For the marketing project now

The QA chat calls `qa.run_capability_preflight(project_id="marketing")` once.
The marketing Planner is woken by the resulting job and approves with the cited
`preflight:` ref. MKT-REWARD-006 is then claimable. A client must reload to see
the new tools; the server-side routing applies immediately.

## Tests

`tests/test_capability_preflight.py` (16) reproduces the cycle and covers each
HOH-278 criterion: evidence without a lease, bound to agent and machine without
its name; exactly one job on retry and on repeated polls; one Planner wake; the
review event's evidence refs; approval from the proven subset only, closing the
job and allowing a normal QA claim; refusal of self-report, unknown, other-agent,
other-machine, undeclared and unproven evidence; remote-machine routing;
configured preflights; role boundaries; and no candidate or verdict change. Two
dispatch tests now approve from a preflight record. The affected modules: 153
passed, and the 7 failures are in the pre-existing set on `main`.
