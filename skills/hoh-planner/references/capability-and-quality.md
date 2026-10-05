# QA capability and quality gates

For a new project, configure repository and test command from the real checkout
before releasing executable work. Inventory actual verification equipment and
configure the verification profile with portable names such as
`build_execution`, `source_inspection`, `runtime_trace`, `browser_interaction`,
`screenshot`, `audio_observation` or a domain adapter.

The project profile is a catalog, not proof that a QA session can use it. QA
declares only tools it verified on its stable `machine_id`, then calls
`qa.run_capability_preflight`: the HoH host probes that machine, needing no
lease, and records a `preflight:<id>`. The server then approves declared and
proven capabilities for that agent and machine itself (`system:host-preflight`);
a change to the declaration, preflight commands, test command or profile revokes
it until the next preflight. `planner.configure_agent_capability_profile` stays
for overrides and accepts only such records, for the same agent and machine.

Built-in probes cover `source_inspection`, `git_worktree`, `dependency_manifest`,
`build_execution`, `browser_interaction` and `screenshot`. For any other
capability configure `planner.configure_capability_preflight` with a command
that exits zero only when the equipment works on the host.

A QA-ready candidate blocked on approval alone opens one Planner job whose
summary names the owner and action:

- `owner=planner action=capability_approval` (rare since wave 7A): call
  `planner.configure_agent_capability_profile` with the proven capabilities and
  the cited `preflight:` ref. Approving closes the job.
- `owner=infrastructure action=capability_preflight_gap`: the preflight cannot
  prove a capability. Provision it, or configure its preflight command, then have
  QA rerun `qa.run_capability_preflight`.

`missing_declaration` on `capability_review_required` is a registration or
provisioning gap, not an approval: never approve an undeclared capability.

Missing runner, browser, fixture, asset tool or tracer is infrastructure work;
it never reduces acceptance. Keep the frozen product candidate intact while
infrastructure is remediated. Link that work to its owning project rather than
sending a product Developer to patch HoH.

Before `planner.set_ticket_ready`, call `planner.configure_validation_contract`
to configure a validation contract that covers
each acceptance criterion exactly once. Each scenario needs stable
`scenario_ref`, `black_box`/`white_box`/`mixed` method, expected observation,
required evidence types and capabilities. A green aggregate suite without these
observations is not enough.
