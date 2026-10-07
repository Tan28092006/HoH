# HoH — entry map for agents

HoH (Harness Control Plane) coordinates separate Planner, Developer, QA and
Integrator agents through a shared MCP service, a SQLite/PostgreSQL state store
and Git worktrees. This file is a map: it holds the rules that apply to every
task and tells you what to read for the rest. Do not add release history or live
ticket state here; `tests/test_repo_docs.py` caps it at 900 words.

## Find your bearings

| Question | Answer |
| --- | --- |
| How is the code organized, and where is an invariant enforced? | [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) |
| Which document covers my task? | [docs/README.md](docs/README.md): every document with a "read when" trigger |
| What is installed on this machine, and what must never happen to it? | [docs/LOCAL_RUNTIME.md](docs/LOCAL_RUNTIME.md) |
| What is the state of the tickets? | Not in any file. Bootstrap the role MCP, call `work.discover`, then resolve the current ticket packet. If the MCP is unreachable, say so; do not reconstruct state from documents or old chat. |
| Why does an invariant exist? | [docs/releases/README.md](docs/releases/README.md) names the release that introduced it |
| What are the known structural risks? | [docs/reviews/2026-10-harness-architecture-review.md](docs/reviews/2026-10-harness-architecture-review.md) |

## Run and verify

- Tests: `powershell -File scripts/test.ps1 [pytest args]`. It pins `.venv-hoh`
  and this checkout's `src/` (`tests/conftest.py` refuses any other tree) and
  runs `-n 6` by default (`-n 0` serial). Only the full suite checks the 80%
  coverage gate; lanes in [docs/TESTING.md](docs/TESTING.md).
- `main` is green (1167 passed, 31 skipped, coverage 83% on 2026-10-05). A failure after your
  change is yours until a run of unchanged `main` shows otherwise.
- A local development instance: [docs/LOCAL_SETUP.md](docs/LOCAL_SETUP.md).
  Never start a component without the host environment (no
  `HOH_DATABASE_PATH` creates a stray database).

## Rules for every task

1. **Data.** Never reset, recreate or migrate `D:/HoH/data/control-plane.db`, and
   never delete evidence, candidates or worktrees under `D:/HoH/data` or
   `D:/HoH/tmp`. Change state only through the service's lifecycle tools.
2. **Installed service.** Changing source does not update `D:/HoH-service`. Work
   on an isolated branch or worktree of the deployed commit; upgrade with
   `deploy/desktop/` scripts.
3. **Credentials** never go into source, logs, release archives or prompts.
4. **Candidates are frozen.** Never edit a submitted candidate. Agents of a
   managed project never patch HoH; HoH defects become HoH tickets.
5. **History is not relabelled.** Operator and external landings are recorded
   as ungated, never as QA approval.
6. **Encode constraints in the platform.** A prerequisite becomes a dependency
   edge, a transition or a tool call, not prose in a context entry.
7. **Read before relaxing.** Before weakening a rule below, read the release note
   that introduced it.

## Protocol invariants

The service enforces these; [ARCHITECTURE.md](docs/ARCHITECTURE.md) says where.

- HoH's own gated merge opens no barrier: the server records `keep` and flags
  open tickets the merged paths reach `planning_base_stale`; their next passed
  QA verdict needs a verified `intent_check`.
- Any other commit observed on the default branch opens a repository
  reconciliation, or extends the one pending; other branches are only recorded.
  It freezes the tickets its paths reach (whole project for docs/spec, root
  files, rewritten history or unknown diffs) until Planner classifies each (keep,
  revise, cancel, supersede, complete, rework); running QA may finish. QA
  rejection, gate regression and drift onto an observed `main` freeze only that
  ticket and its dependents; the server resolves the first two unless the
  rework budget is spent.
- Planner advises asynchronously: nothing routine waits on it, yet it can
  stop, revise or retire any ticket. Its sync fetches every remote head with
  prune but never checks out, pulls, merges or rebases the shared checkout;
  obsolete tickets retire through the pending reconciliation, keeping their
  worktrees, candidates and evidence.
- Every open ticket has an explicit validation contract. A candidate is one
  ordinary commit on the frozen base or the prior assessed candidate; a server
  rebase on drift derives one on current main that lands only after the frozen
  suite passes on it. QA independently runs the declared scenarios on the
  frozen candidate and attaches every required evidence type. A gated merge
  or ungated landing creates one `delivery_completed` Planner report and a
  check of `main`; a red check or spent budget pauses the merge queue.
- QA claim authority is declared capabilities a host preflight proved on the
  same machine (stable hostname `machine_id`), server-approved; Planner may
  override. Missing capability is an infrastructure gap, never grounds to
  weaken acceptance. QA's `model_family` must differ from the maker's. Merge needs `adversarial_review` evidence at
  medium risk, plus `policy.approve_high_risk` at high.
- `dev.submit_candidate`, `qa.submit_verdict` and `integrator.merge_candidate`
  require the `context_snapshot_id` of a packet read just before the write.
- Packets are bounded (4,000 estimated tokens by default; contract fields 600
  characters, contract 1,200 tokens). Older history is resolved by reference.
- Planner syncs repository truth after bootstrap and before every idle wait,
  and keeps at most three current priorities. Execution roles report one
  unhandled failure through `work.report_failure` before idling.

## Role skills

Role behavior lives in `skills/<role>/SKILL.md` and its `references/`. These are
product code shipped in the wheel. Change them
with `tests/test_skill_*.py`, and keep each card short (release 0.3.19).

## Before you finish

- Run the tests for what you changed and say which checks you did not run.
- If you changed a fact, update the one document that owns it (the map above);
  do not copy it here.
- A release gets `docs/releases/RELEASE_<version>.md` and a row in that index.
