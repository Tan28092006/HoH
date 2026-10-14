# Full lane

The full regression run includes current tests and clears the existing 80 percent
coverage threshold with branches measured.

## Sub-features

- `canonical`: full wrapper rejects weak coverage, stale append and foreign config.
- `full`: no marker or path filtering; real native exit and branch XML retained.

## How to get to it (user POV)

Run `scripts/test.ps1 -n 2` with empty PYTEST_ADDOPTS. Independent HoH QA runs its
frozen declared suite once; Developer does not duplicate that acceptance run.

## Driving it with control-hoh-ci

Preconditions: QA/coordinator owns the full-suite slot and ample time; new paths.

```powershell
& 'D:/HoH/.venv-hoh/Scripts/python.exe' scripts/verify-ci-evidence.py run --lane full --workers 2 --evidence D:/.hoh-pytest/verify-hoh-ci-full --basetemp D:/.hoh-pytest/verify-hoh-ci-full-temp
```

Require process success, positive passes, all original cases accounted for,
authorized exclusions, coverage.xml with actual branches and combined coverage
at least80, and matching manifest revision/source. Keep all evidence if red.

## Gotchas

Historical red baselines do not turn green because coverage clears80. This
recipe is a suite invocation, not permission to overlap QA, retry until green,
or claim Windows/Ubuntu/Python3.12/3.13 from one local interpreter.
