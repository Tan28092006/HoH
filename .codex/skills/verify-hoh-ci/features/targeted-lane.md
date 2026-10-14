# Targeted lane

Developers run one named scenario with the supported pinned wrapper and receive
its native result without a misleading full-suite coverage claim.

## Sub-features

- `identity`: doctor observes the interpreter, source, PID and revision.
- `drive`: one actual health/board scenario uses real API and isolated SQLite.
- `cleanup`: owned children and scratch end; evidence remains.

## How to get to it (user POV)

Run `scripts/test.ps1 tests/test_api.py::test_health_board_and_security_headers -n 0`
from the candidate checkout. The helper below adds private paths and evidence.

## Driving it with control-hoh-ci

Preconditions: exclusive test slot, existing pinned venv, PowerShell available.

```powershell
& 'D:/HoH/.venv-hoh/Scripts/python.exe' .codex/skills/verify-hoh-ci/scripts/control.py verify --evidence-root D:/.hoh-pytest/verify-hoh-ci-proof --scratch-root D:/.hoh-pytest/verify-hoh-ci-scratch
```

Require native zero, exactly the named passing JUnit case, matching doctor source,
one demo project and project.created event in the retained fixture-proof.db,
and cleanup.json with scratch_removed true. Read the action and result logs.

## Gotchas

This is a short-lived CLI; no port is expected. TestClient/SQLite proof does not
prove installed host, browser, PostgreSQL or the full regression suite. The
helper never points serving components at a real database.
