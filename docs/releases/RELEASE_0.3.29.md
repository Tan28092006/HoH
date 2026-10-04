# HoH 0.3.29 — less ceremony, cheaper context

Wave 2 of the [2026-10 architecture review](../reviews/2026-10-harness-architecture-review.md):
fewer wasted tokens and round trips for agents, less to keep in sync.

## Agent interface

- **H3 — MCP errors keep their details.** Errors read
  `[code] message {"details":...,"retry_hint":...}`; the JSON is omitted when
  empty. Hints cover stale context, the reconciliation barrier and lease expiry.
  MCP and REST details are tested for parity.
- **H4 — every cited reference resolves for its role.** `context.resolve` now
  answers `ctx:ticket:<id>`, `ctx:ticket:<id>[:vN]:<section>`,
  `ctx:feature:<id>[:vN][:<section>]`. Execution roles may resolve history,
  filtered to the entry types their packet carries.
- **H5 — no re-sending unchanged packets.** `ticket.get_context` accepts
  `known_snapshot_id` and answers `{unchanged: true}` when nothing moved. Claims
  return `current_context_snapshot_id`, so a write can follow a claim without a
  full packet read.
- **H6 — budgets measured on what is sent.** MCP returns compact JSON text and
  the token estimate uses that same text. `work.discover` returns only
  `agent {id, role}` and `project {id, name}`; bootstrap already carries the rest.

## Board hygiene

- **H12 — directory scopes work (HOH-279).** `dir/` is stored as `dir/**`, and a
  legacy `dir/` row matches as `dir/**`.
- **H11 — experience expires.** Notes record the base commit, stop surfacing
  after 30 days unseen, rank by recency, and Planner can `experience.retire` one.
- **H1 — HoH measures its loop.** `planner.cycle_metrics` and
  `GET /api/v1/projects/{id}/cycle-metrics` report per-ticket phase durations
  (median, p90), QA runs and gate runs executed vs reused, from existing events.

## Removed ceremony

- **Semantic decision records removed** (`semantic_record.py`,
  `commit_semantic_record`, `docs/SEMANTIC_LAYER.md`). The queued merge never
  wrote them.
- **H9 — hand-written client configs removed** (`deploy/codex`,
  `deploy/claude-code`, `deploy/antigravity`). Configs come only from
  `local_agent setup` and `hoh-agent-kit`.
- **H10 — one statement of the wait protocol**, in the `work.wait_for_event`
  tool description; skills and generated prompts point at it, and a test refuses
  restated constants.

Schema: four nullable columns on `experience_notes` (additive). No data changes.

## Verification

Full suite, six workers: 1033 passed, 30 skipped, 0 failed. Coverage gate and
PostgreSQL paths not run.
