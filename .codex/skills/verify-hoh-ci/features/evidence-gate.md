# Evidence gate

CI accepts actual completed lane evidence and refuses missing, substituted,
failed, empty or unexpectedly skipped reports.

## Sub-features

- `validate`: source, revision, lane, native result, digest and case accounting.
- `reject`: unknown skips, all-skipped, malformed and insufficient coverage.

## How to get to it (user POV)

Each CI driver run validates before returning success. For read-only rechecking,
use `scripts/verify-ci-evidence.py validate` on a retained lane directory.

## Driving it with control-hoh-ci

Preconditions: evidence was created by the real `run` command. In PowerShell:

```powershell
$evidenceRevision = git rev-parse HEAD
& 'D:/HoH/.venv-hoh/Scripts/python.exe' scripts/verify-ci-evidence.py validate --lane fast --evidence D:/.hoh-pytest/verify-hoh-ci-fast --expected-revision $evidenceRevision
```

Require zero plus the exact pass/skip inventory. Copy evidence into an owned
scratch folder before negative controls; missing XML, wrong revision, failed
native status, unknown skipped security case and branchless/below80 full report
must return nonzero with a concrete reason. Retain both command and result.

## Gotchas

Synthetic reports prove validator rejection behavior only. They do not prove
production test execution. A recheck never reruns tests, changes an artifact,
or upgrades an old report to a different revision.
