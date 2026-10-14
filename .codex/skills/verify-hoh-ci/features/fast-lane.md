# Fast lane

Pull requests run the existing fast population and retain passing and excluded
case identities for review.

## Sub-features

- `selection`: exact marker expression `not slow and not git`.
- `evidence`: native process status, source/revision and bounded prerequisite skips.

## How to get to it (user POV)

Run `scripts/test.ps1 --fast -n 2` locally. CI uses the repository evidence driver.

## Driving it with control-hoh-ci

Preconditions: exclusive test slot; choose new private paths for each run.

```powershell
& 'D:/HoH/.venv-hoh/Scripts/python.exe' scripts/verify-ci-evidence.py run --lane fast --workers 2 --evidence D:/.hoh-pytest/verify-hoh-ci-fast --basetemp D:/.hoh-pytest/verify-hoh-ci-fast-temp
```

Require native zero and inventory.json with positive passes, exact revision,
case identities and explicit authorized skip reasons. The driver closes its
owned process tree; retain the evidence directory. Preserve basetemp on failures.

## Gotchas

The evidence directory must not already exist. Fast is not full coverage proof.
Never add a marker signature to remove an expensive existing test as a speedup.
