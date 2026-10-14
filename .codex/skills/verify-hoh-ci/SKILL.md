---
name: verify-hoh-ci
description: Drive HoH's supported test CLI and retained CI evidence gate with pinned source, disposable fixture databases and owned subprocesses. Use after changing test runners or CI gates.
---

# Verify HoH's test CLI

Read [the feature map](features/README.md). Obtain the coordinator's exclusive
test slot before any doctor or drive; use at most two local workers. This skill
never starts the installed host, changes shared credentials or opens its database.

## Launch

This surface is a short-lived CLI. It has no server or port to keep alive.
Use the existing shared test interpreter; never bootstrap from a ticket worktree.
From the candidate checkout on this Windows host:

```powershell
& 'D:/HoH/.venv-hoh/Scripts/python.exe' .codex/skills/verify-hoh-ci/scripts/control.py verify --evidence-root D:/.hoh-pytest/verify-hoh-ci-proof --scratch-root D:/.hoh-pytest/verify-hoh-ci-scratch
```

The helper allocates a fresh run ID, runs doctor, drives one real targeted test
through `scripts/test.ps1 -n 0`, captures its isolated fixture database, and cleans
up. It uses `run_managed` for the repo's Windows Job Object/POSIX group ownership.
There is no persistent PID to stop; every drive ends its owned subprocess tree.

## Doctor

```powershell
& 'D:/HoH/.venv-hoh/Scripts/python.exe' .codex/skills/verify-hoh-ci/scripts/control.py doctor --evidence-root D:/.hoh-pytest/verify-hoh-ci-proof --scratch-root D:/.hoh-pytest/verify-hoh-ci-scratch
```

Doctor executes the actual pinned interpreter and reads its PID, pytest version,
HoH version, package source and checkout SHA. It rejects a foreign interpreter or
source. Read `doctor.json` and `doctor.log`; a metadata timestamp is not proof of
identity. For this CLI, the probe process completes and there is no listening port.

## Drive

Use `verify` above for [targeted lane](features/targeted-lane.md). For fast/full
lanes and artifact rejection controls, follow their exact recipes. Do not infer
full suite acceptance from a targeted test or a synthetic report.

## Evidence

The printed absolute run directory survives cleanup. It contains the doctor
command/results, actual drive argv/native status, pytest transcript and JUnit,
public API health/board test outcome, and a read-only SQLite backup with the
demo project and its creation audit event. `feature-proof.json` records those
side effects. `cleanup.json` records scratch removal and retained filenames.
The targeted API test uses real transport/storage through TestClient; it does not
prove a listening server, browser UI, PostgreSQL or a different Python minor.

CI evidence runs retain `manifest.json`, `pytest.log`, `tests.xml`,
`inventory.json`, and full-run `coverage.xml`. Failed or incomplete evidence is
rejected; keep it for diagnosis. Compare actual case IDs/outcomes/skip reasons,
commands and revisions before comparing elapsed time.

## Cleanup

The helper always closes its owned process tree and removes only its fresh,
verified scratch directory. It refuses candidate, `D:/HoH/data`, `D:/HoH/tmp`
and installed-service paths. It never deletes proof artifacts. After any run,
require `cleanup.json`, confirm scratch is absent and evidence files remain.
Never kill a process by name or reuse a saved PID as authority.

## Helpers

`scripts/control.py doctor` and `scripts/control.py verify` are executable with
the pinned Python commands above. `scripts/verify-ci-evidence.py` is the real
repository CI driver/validator; the map documents its entry points. Update this
map when those CLI or API contracts change.
