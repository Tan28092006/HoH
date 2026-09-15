# QA capability and quality gates

For a new project, configure repository and test command from the real checkout
before releasing executable work. Inventory actual verification equipment and
configure the verification profile with portable names such as
`build_execution`, `source_inspection`, `runtime_trace`, `browser_interaction`,
`screenshot`, `audio_observation` or a domain adapter.

The project profile is a catalog, not proof that a QA session can use it. QA
first declares only tools actually verified on its stable `machine_id`. Planner
then independently inspects public preflight evidence and configures an approved
subset bound to that same machine. Never approve from QA self-report. Revoke
unavailable equipment through a smaller evidenced approval.

On `capability_review_required`:

- `missing_declaration`: the QA session has not established callable equipment.
  Provision or route it, then ask QA to verify and re-register. Never approve
  an undeclared capability.
- `missing_approval`: declaration exists but lacks independent public evidence
  on that `machine_id`. Inspect the preflight record and approve only proven
  capability names.

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
