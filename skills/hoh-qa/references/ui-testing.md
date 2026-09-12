# Candidate UI verification

Use for rendered UI or interaction changes. Source inspection, HTTP 200 and passing
API tests do not establish a user journey.

If the project provides a Planner-frozen browser command, run it through
`qa.run_project_tests` and poll the same test-run ID. Verify the report names the
candidate SHA, fixture/environment and actual browser scenarios; a backend-only
run or a skipped browser suite cannot verify UI criteria. The HoH repository's
`docs/UI_QA.md` documents its optional runner; that repository document is not
part of the installed skill. Other projects supply their own browser suite.

1. Use the approved launch interface for a candidate-specific instance. Verify
   build/source SHA, URL/port and isolated fixture DB. The existing operator tab
   on main is not candidate evidence. If identity cannot be proven, record
   insufficient evidence. Never alter live tickets for fixtures or fault injection.
2. Use the host's available browser automation and its instructions. Prefer
   accessible role/name locators and observable waits. Missing browser capability
   is an evidence gap; never simulate a successful interaction.
3. Map each UI criterion to entry -> action -> visible state -> expected result.
   Exercise relevant navigation, filter combinations, back/reload/deep links and
   retention. Assert rendered behavior and backing data, not just screenshots.
4. Select relevant empty/loading/error/success, long/Unicode and populated-list
   fixtures. Test repeated actions, delayed/out-of-order responses and stale state
   when affected. Group scenarios sharing setup.
5. Check supported viewport sizes; if unspecified use desktop and one narrow size
   for responsive UI and record dimensions. Inspect clipping, overlap, text,
   controls and scrolling. Check keyboard navigation, visible focus, labels and
   dialog focus/escape where applicable. Do not claim full accessibility compliance
   from a brief inspection.
6. Retain assertions, relevant console/network failures and screenshots/trace at
   key checkpoints. Artifacts name ticket/SHA/scenario, fixture/environment and
   viewport. Redact secrets. Clean up only this run's test resources.

HoH board examples: project isolation, feature/status filters including Backlog,
ticket dependencies, agent/role/lease display, evidence links and polling updates.
Read-only criteria require no state-changing requests or embedded bearer tokens.
Select checks matching the ticket; send unrelated gaps to Planner separately.
