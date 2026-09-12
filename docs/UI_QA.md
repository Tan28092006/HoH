# Browser QA on a candidate

HoH accepts a Planner-frozen test command; that command can run a browser suite.
The QA role queues it with `qa.run_project_tests`, retains the run ID and polls
`qa.get_project_test_run`. Tests passing are evidence for the criteria they actually
exercise; QA still records criterion findings and an independent verdict.

Install once on the runner:

```sh
python -m pip install -e '.[dev,ui]'
python -m playwright install chromium
```

For this repository, configure a command equivalent to:

```sh
python -m pytest -c pyproject.toml tests/ui --run-ui --no-cov --ui-artifacts <absolute-evidence-directory>
```

Use the runner's Python executable as the first argv element; no shell string.
Keep `-c pyproject.toml` explicit: an artifact path outside a linked worktree can
otherwise affect pytest's root discovery and select the parent repository's config.
An installed Chrome can be selected with `--ui-channel chrome`, which avoids the
separate Chromium download. A combined project gate can omit `tests/ui` and run
the full suite with `--run-ui`. Do not claim a UI gate ran if `--run-ui` was absent.

The suite starts the candidate's API on a temporary loopback port with a fresh
fixture DB. It asserts that imports resolve under that checkout, records its Git
HEAD and whether the working tree is dirty, and never attaches to the live board.
`/__qa_identity` exists only on this test app, not in the deployed API.
An uncommitted development run is explicitly marked dirty; release QA requires
a clean frozen candidate.

Each scenario retains a full-page screenshot, Playwright trace, browser/viewport,
request methods, JS/HTTP errors, source SHA, fixture identity and artifact SHA-256
checksums. The root must be outside the candidate checkout. Missing Playwright or
browser binaries fail an enabled UI run; normal backend-only runs skip UI explicitly.

Coverage: desktop/tablet/narrow viewport; backlog and feature filters; reload;
ticket context; navigation; basic keyboard reachability; horizontal overflow;
read-only requests; empty and error state; delayed stale snapshot handling.
This is not a complete accessibility audit or automatic visual-baseline comparison.

Other UI projects register their own browser command with equivalent isolation:
launch/build the frozen SHA, use fixture data, drive actual interactions, retain
assertions and artifacts. External artifact storage and remote browser sessions
need an operator-provided adapter. Do not use a mainline/live URL as candidate evidence.
