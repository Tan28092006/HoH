# The installed local runtime

This machine runs one shared HoH host that serves every Codex, Claude Code and
Antigravity role profile. This page says where it lives, what must never happen
to it, and how to check its current state. It deliberately does not record live
ticket state or the installed version as a fact: both change, and a stale copy
here once sent agents to the wrong release. Use the commands below instead.

## Where things are

| Thing | Location | Check |
| --- | --- | --- |
| Installed service | `D:/HoH-service` (host: `hoh_desktop.py --host`, started by `start-host.ps1`) | — |
| Installed version | Newest `D:/HoH-service/config/upgrade-<version>-<timestamp>` directory | `ls -t D:/HoH-service/config \| grep upgrade- \| head -1` |
| Release archives and validation records | `D:/HoH-service/release/` | `HoH-<version>-validation.md` where one was written |
| Authoritative database | `D:/HoH/data/control-plane.db` | `database_path` in `D:/HoH-service/config/settings.json` |
| Evidence, ticket worktrees, recovery patches | `D:/HoH/data/` | `git worktree list` from `D:/HoH` |
| Service configuration and credentials | `D:/HoH-service/config/` | never copy into source, logs or archives |

`D:/HoH/data/control_plane.db` (underscore, empty) and
`D:/HoH-service/data/control-plane.db` are not the authoritative database, and
neither are the `accidental-diagnostic-*` and `*-before-*` files in
`D:/HoH-service/config/`. Never point a component at them.

## Never

- **Never reset, recreate or migrate the authoritative database** as part of an
  upgrade or a test. Use the service's supported lifecycle for state changes.
  Upgrades back up configuration under `D:/HoH-service/config/upgrade-*`.
- **Never run a component without the host environment.** With no
  `HOH_DATABASE_PATH` a component creates a stray database relative to its
  working directory, and agents then read an empty board.
- **Never put installation or client credentials** in source, logs, release
  archives or this repository.
- **Never delete candidate worktrees, evidence or historical records**, including
  the HOH-256 RED worktree and the worktrees under `D:/HoH/tmp/`. Old launcher
  edits are preserved byte-for-byte in `D:/HoH/tmp/local-launcher-preservation`;
  do not reapply them over mainline.
- **Never edit a frozen candidate**, and never make a managed project's agents
  (for example LegalGenerator's) patch HoH. HoH defects become HoH tickets.
- **Never relabel history.** The production `merge_job_427cf10ad5684b94` moved the
  correct Legal SHA with an old candidate ID and skipped the isolated gate; it
  stays recorded as it happened. Operator landings are recorded as ungated
  (`planner.record_external_landing`), not as independent QA approval.

## Host settings that must survive upgrades

- `start-host.ps1` sets `OPENBLAS_NUM_THREADS=1` and `OMP_NUM_THREADS=1`. With
  them, production Legal QA passed 555 tests (5 skipped) at about 191 MB peak
  child commit after an earlier 3.38 GB allocation failure. This is host
  configuration, not a change to any project's source or frozen command.
- The desktop launcher starts the host outside the caller's process job, so a
  Codex restart does not stop the shared endpoint.
- Generated setup commands use `python -I -m` inside a fenced block (0.3.23).

## Clients

All installed Codex and Claude profiles use authenticated shared HTTP. A profile
created after Codex app-server started is invisible to it: keep HoH running,
fully exit Codex desktop and reopen it. Reloading a task or renderer is not
enough. After reconnecting, bootstrap and resolve the newest ticket packet before
acting; earlier chat context is historical.

## Upgrading

Changing source does not update the installed service. Build and upgrade from an
isolated branch or worktree based on the deployed commit, with
`deploy/desktop/package_local.py` and `deploy/desktop/upgrade_local.py`. The
0.3.23 upgrade restart once failed with no captured cause and succeeded from the
desktop icon; the validation record describes it.

## Development environment traps

- `D:/HoH/.venv-hoh` is the working interpreter. Its editable install points at
  an old worktree (`tmp/planner-merge-handoff`), so tests must run with
  `PYTHONPATH` naming the checkout under test. `scripts/test.ps1` does this, and
  `tests/conftest.py` refuses to run against any other source tree.
- `D:/HoH/.venv` and a bare `python` resolve to an msys interpreter without
  pytest; the system Python lacks `pytest-cov`. Neither can run the suite.
