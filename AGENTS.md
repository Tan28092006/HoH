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
  and puts this checkout's `src/` first; `tests/conftest.py` refuses to run
  against any other source tree. It runs across workers (`-n 6` by default, about 5
  minutes for the full suite; `-n 0` for serial). Targeted runs may add
  `--no-cov`; the full suite must clear the 80% coverage gate.
- `main` is green (1167 passed, 31 skipped, coverage 83% on 2026-10-05). A failure after your
  change is yours until a run of unchanged `main` shows otherwise.
- A local development instance: [docs/LOCAL_SETUP.md](docs/LOCAL_SETUP.md).
  Never start a component without the host environment: with no
  `HOH_DATABASE_PATH` it creates a stray database.

## Rules for every task

1. **Data.** Never reset, recreate or migrate `D:/HoH/data/control-plane.db`, and
   never delete evidence, candidates or worktrees under `D:/HoH/data` or
   `D:/HoH/tmp`. Change state only through the service's lifecycle tools.
2. **Installed service.** Changing source does not update `D:/HoH-service`. Work
   on an isolated branch or worktree based on the deployed commit; upgrade with
   the packaging scripts in `deploy/desktop/`.
3. **Credentials** never go into source, logs, release archives or prompts.
4. **Candidates are frozen.** Never edit a submitted candidate. Agents of a
   managed project never patch HoH; HoH defects become HoH tickets.
5. **History is not relabelled.** Operator or external landings are recorded
   as ungated (Planner, or the sweep as `system:sweep`), never as QA approval.
6. **Encode constraints in the platform.** A prerequisite becomes a dependency
   edge, a transition or a tool call, not prose in a context entry.
7. **Read before relaxing.** Before weakening a rule below, read the release note
   that introduced it.

## Protocol invariants

The service enforces these; [ARCHITECTURE.md](docs/ARCHITECTURE.md) says where.

- HoH's own gated merge opens no barrier: the server records `keep` and flags
  open tickets the merged paths reach (scope overlap, transitive dependency,
  docs/spec or root file) `planning_base_stale`; their next passed QA verdict
  needs a verified `intent_check`. Planner gets an advisory event.
- Any other observed commit on the default branch opens a repository
  reconciliation, or extends the one pending sync observation; other branches
  are recorded only. It freezes only the tickets that closure reaches (whole
  project for docs/spec, root files, rewritten history or unknown diffs).
  Until Planner classifies each captured ticket (keep, revise, cancel,
  supersede, complete, rework) and resolves it, nothing new starts for it;
  running QA may finish. QA rejection, integration regression and drift onto an
  observed `main` freeze only that ticket and its dependents; the server
  resolves a rejection's or regression's itself, except a spent rework budget.
- Planner is an asynchronous advisor: nothing routine waits on it, yet it can
  stop, revise or retire any ticket anytime.
- Planner sync fetches every remote head with prune but never checks out, pulls,
  merges or rebases the shared checkout. An obsolete ticket is retired through
  the pending reconciliation, which preserves its worktrees, candidates and
  evidence.
- Every open ticket has an explicit validation contract. A candidate is exactly
  one ordinary commit whose parent is the frozen base or the prior assessed
  candidate; a derived candidate (server rebase on drift) is that patch on
  current main, landing only after the frozen suite passes on it. QA
  independently executes the declared scenarios on the frozen candidate and
  attaches every required evidence type. A gated merge or ungated landing
  creates one durable `delivery_completed` Planner report and a check of
  `main`; a red check or an exceeded budget pauses the merge queue.
- QA claim authority is live declared capabilities intersected with what a
  host preflight proved on the same machine (QA registers a stable hostname as
  `machine_id`); the server approves it, Planner may override. Missing
  capability is an infrastructure gap, never a reason to weaken acceptance.
  QA's registered `model_family` must differ from the maker's
  (`checker_distinct_from_maker`). Merge needs `adversarial_review` evidence at
  medium risk, plus `policy.approve_high_risk` at high.
- `dev.submit_candidate`, `qa.submit_verdict` and `integrator.merge_candidate`
  require the `context_snapshot_id` of a packet read just before the write.
- Packets are bounded (4,000 estimated tokens by default; contract fields 600
  characters, contract 1,200 tokens). Older history is resolved by reference.
- Planner syncs repository truth after bootstrap and before every idle wait,
  and keeps at most three current priorities. Execution roles report one
  unhandled failure through `work.report_failure` before going idle.

## Role skills

Role behavior lives in `skills/<role>/SKILL.md` and its `references/`. These are
product code: they ship in the wheel and reach clients on upgrade. Change them
with `tests/test_skill_*.py`, and keep each card short (release 0.3.19).

## Before you finish

- Run the tests for what you changed and say which checks you did not run.
- If you changed a fact, update the one document that owns it (the map above);
  do not copy it here.
- A release gets `docs/releases/RELEASE_<version>.md` and a row in that index.
