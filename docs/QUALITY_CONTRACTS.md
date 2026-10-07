# Quality contracts

> Snapshot 2026-09-14, not kept in step with the code. QA capability approval is
> now granted by the server from host preflight evidence, and risk tiers and
> maker/checker model families were added later; [ARCHITECTURE.md](ARCHITECTURE.md)
> and the code win where they disagree.

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

The project profile is the intended harness surface. Each QA presence separately
registers `execution_capabilities` that the current session has actually checked.
A Planner or operator then inspects durable public preflight records and calls
`planner.configure_agent_capability_profile`; self-report alone never grants claim
authority. Strict work is offered and claimable only when every required capability
is both live-declared and independently approved. This prevents a browser or
runtime-trace ticket from being assigned to a machine that merely copied the
project declaration. A later `agent.register` updates the live declaration but
cannot overwrite the approval. Approval is bound to `machine_id`; moving the
same agent identity to another host requires a new preflight and approval.

An unoffered QA queue is still observable. `work.inbox.capability_blockers` and
the matching discovery `capability_state` separate `missing_declaration` from
`missing_approval`. HoH also wakes Planner with the same bounded fields. QA
verifies callable equipment and re-registers; Planner provisions missing
equipment or approves only public-evidence-backed capabilities on that machine.
The candidate remains frozen and queued throughout this control exchange.
When another online QA already satisfies the contract, HoH routes work there and
suppresses the unnecessary capability wakeup for the mismatched identity.

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

Capabilities belong to the project contract, the approved QA profile and the
current QA presence;
evidence still records the actual environment digest and frozen candidate SHA.
A machine that lacks a required capability does not receive or claim the ticket.
If a capability disappears during a run, QA records `insufficient_evidence`.
Planner adds or routes the required runner instead of weakening the ticket.
`colocated` and `agent_workspace`
worktree rules are unchanged: the validation contract never grants filesystem
access or permission to reuse another agent's mutable worktree.

Projects upgraded from older HoH releases start in `legacy` quality policy so
historical tickets and evidence remain valid. Planner should contract current
READY work first, then enable `claim-evidence-v1`.
