# HoH 0.3.30 — the large modules are split

Wave 3 (P2) of the [2026-10 architecture review](../reviews/2026-10-harness-architecture-review.md).
Moves and extractions only; no behaviour change.

- `mcp_server.py` (2,073 lines) became the `hoh_control_plane.mcp` package:
  `invoke`, `workers` (one spawn helper), `server`, `http_app` and per-role
  `tools/`. The old module keeps `main` and its exported names.
- `service/planning.py` (3,363 lines) became eight mixins: project setup,
  repository reconciliation, project policy, ticket authoring, ticket retirement,
  candidate recovery, requirements and Planner context.
- `store.py` keeps the connection seam; the DDL is in `store_schema.py` and the
  migrations in `store_migrations.py`.
- The reconciliation barrier is read through one `ExecutionGate`.
- `scripts/test.ps1` defaults to six workers; more grew the Windows pagefile
  until the system drive filled.

## Verification

`scripts/test.ps1`: 1053 passed, 30 skipped, 0 failed.
