# Quality contracts

HoH maps the Harness-of-Harness development document to durable project and
ticket state. This keeps the same quality loop when Planner, Developer and QA
run in separate chats or on separate machines.

## Project verification profile

Planner calls `planner.configure_verification_profile` once the real project
environment is known. `capabilities` describes observable equipment available
to QA, for example:

- `build_execution` and `source_inspection`
- `runtime_trace`
- `browser_interaction` and `screenshot`
- `audio_observation`
- domain adapters such as an engine MCP, hardware runner or dataset fixture

With `quality_policy=claim-evidence-v1`, HoH refuses a READY ticket whose
validation contract needs an undeclared capability. Enabling this policy also
checks tickets already in READY so the migration cannot strand work behind an
implicit test plan.

## Ticket validation contract

Planner attaches one contract entry per acceptance criterion, either during
`planner.create_ticket` or with `planner.configure_validation_contract`:

```json
{
  "criterion": "A signed-in user can save the form",
  "scenario_ref": "ui:save-form-happy-path",
  "method": "mixed",
  "expected_observation": "The saved values survive reload and a success message is visible",
  "required_evidence_types": ["browser_trace", "screenshot", "test_result"],
  "required_capabilities": ["browser_interaction", "screenshot"]
}
```

Criterion text must match the ticket exactly and every criterion appears once.
Changing criteria on a contracted ticket requires a replacement contract in the
same update. A READY ticket remains valid only when its project profile supplies
all capabilities.

Developer uses the contract to produce public execution records. Those records
are inputs for QA and do not establish acceptance. QA independently reproduces
each scenario on the frozen candidate. The backend rejects evidence with a
different scenario reference or missing required evidence type.

`qa.submit_verdict` returns an evidence bundle containing verified records, gap
records and a Planner handoff. Ticket context materializes the latest candidate's
bundle so Integrator and a replacement session can audit the result without the
originating chat.

## Multi-machine rule

Capabilities belong to the project contract; evidence still records the actual
environment digest and frozen candidate SHA. A machine that lacks a required
capability reports `insufficient_evidence`. Planner adds or routes the required
runner instead of weakening the ticket. `colocated` and `agent_workspace`
worktree rules are unchanged: the validation contract never grants filesystem
access or permission to reuse another agent's mutable worktree.

Projects upgraded from older HoH releases start in `legacy` quality policy so
historical tickets and evidence remain valid. Planner should contract current
READY work first, then enable `claim-evidence-v1`.
