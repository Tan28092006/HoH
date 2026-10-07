from __future__ import annotations

import pytest

from hoh_control_plane.domain import Role, ValidationError

from helpers.qa import candidate_waiting_for_qa as _candidate_waiting_for_qa


CAPABILITIES = ["build_execution", "runtime_trace", "source_inspection"]


def _contract(criteria: list[str]) -> list[dict[str, object]]:
    return [
        {
            "criterion": criterion,
            "scenario_ref": f"scenario:criterion-{index}",
            "method": "mixed",
            "expected_observation": criterion,
            "required_evidence_types": ["test_result", "runtime_trace"],
            "required_capabilities": ["build_execution", "runtime_trace"],
        }
        for index, criterion in enumerate(criteria, start=1)
    ]


def test_strict_project_refuses_ready_without_a_validation_contract(
    service, actors, seeded
) -> None:
    seeded_ticket = seeded["ticket"]
    service.configure_validation_contract(
        actors["planner"], seeded_ticket["ticket_id"], seeded_ticket["version"],
        _contract(seeded_ticket["acceptance_criteria"]), "Migrate existing ready work.",
    )
    service.configure_verification_profile(
        actors["planner"],
        "project-1",
        capabilities=CAPABILITIES,
        quality_policy="claim-evidence-v1",
    )
    ticket = service.create_ticket(
        actors["planner"], feature_id="payment-retry", ticket_id="DEV-143",
        title="A ticket without an evidence plan", objective="Demonstrate the gate",
        required_role=Role.DEVELOPER, acceptance_criteria=["Observable result"],
        preservation_constraints=[], file_scope=["src/new/**"],
    )

    with pytest.raises(ValidationError, match="validation contract"):
        service.set_ticket_ready(
            actors["planner"], ticket["ticket_id"], ticket["version"]
        )


def test_planner_contract_is_exact_and_reaches_every_role_context(
    service, actors, seeded
) -> None:
    ticket = seeded["ticket"]
    configured = service.configure_validation_contract(
        actors["planner"],
        ticket["ticket_id"],
        ticket["version"],
        _contract(ticket["acceptance_criteria"]),
        "Bind each public requirement to reproducible evidence.",
    )

    for role in ("developer", "qa", "integrator"):
        packet = service.get_ticket_context(actors[role], ticket["ticket_id"], 60_000)
        assert packet["ticket"]["validation_contract"] == configured["ticket"][
            "validation_contract"
        ]
        # Since 0.3.18 only the roles that verify (QA, Planner) carry the profile.
        if role == "qa":
            assert packet["verification_profile"]["quality_policy"] == "legacy"
        else:
            assert packet["verification_profile"] is None


def test_contract_rejects_missing_criteria_and_unavailable_capabilities(
    service, actors, seeded
) -> None:
    seeded_ticket = seeded["ticket"]
    existing_contract = _contract(seeded_ticket["acceptance_criteria"])
    for scenario in existing_contract:
        scenario["required_evidence_types"] = ["source_snapshot"]
        scenario["required_capabilities"] = ["source_inspection"]
    service.configure_validation_contract(
        actors["planner"], seeded_ticket["ticket_id"], seeded_ticket["version"],
        existing_contract, "Migrate existing ready work.",
    )
    ticket = service.create_ticket(
        actors["planner"], feature_id="payment-retry", ticket_id="DEV-144",
        title="Runtime capability gate", objective="Require declared QA equipment",
        required_role=Role.DEVELOPER,
        acceptance_criteria=seeded["ticket"]["acceptance_criteria"],
        preservation_constraints=[], file_scope=["src/capability/**"],
    )
    with pytest.raises(ValidationError, match="exactly once"):
        service.configure_validation_contract(
            actors["planner"],
            ticket["ticket_id"],
            ticket["version"],
            _contract(ticket["acceptance_criteria"][:1]),
            "Incomplete contract.",
        )

    service.configure_verification_profile(
        actors["planner"], "project-1", capabilities=["source_inspection"],
        quality_policy="claim-evidence-v1",
    )
    configured = service.configure_validation_contract(
        actors["planner"],
        ticket["ticket_id"],
        ticket["version"],
        _contract(ticket["acceptance_criteria"]),
        "Contract needs runtime observations.",
    )
    with pytest.raises(ValidationError, match="capabilities"):
        service.set_ticket_ready(
            actors["planner"], ticket["ticket_id"], configured["ticket"]["version"]
        )


def test_qa_cannot_verify_with_the_wrong_scenario_or_evidence_type(
    service, actors, seeded, tmp_path
) -> None:
    ticket = seeded["ticket"]
    configured = service.configure_validation_contract(
        actors["planner"],
        ticket["ticket_id"],
        ticket["version"],
        _contract(ticket["acceptance_criteria"]),
        "Require candidate-bound runtime records.",
    )
    seeded["ticket"] = configured["ticket"]
    ticket, candidate, qa_claim = _candidate_waiting_for_qa(
        service, actors, seeded, tmp_path
    )

    with pytest.raises(ValidationError, match="scenario_ref"):
        service.submit_qa_verdict(
            actors["qa"],
            ticket["ticket_id"],
            qa_claim["lease"]["lease_id"],
            candidate["candidate"]["candidate_id"],
            qa_claim["ticket"]["version"],
            "failed",
            [
                {
                    "criterion": ticket["acceptance_criteria"][0],
                    "status": "failed",
                    "severity": "high",
                    "observation": "Reproduced a regression.",
                    "evidence": [
                        {
                            "type": "screenshot",
                            "observation": "A frame from an unrelated path.",
                            "storage_ref": "evidence://qa/frame.png",
                            "scenario_ref": "scenario:wrong",
                        }
                    ],
                }
            ],
            context_snapshot_id=service.get_ticket_context(actors["qa"], ticket["ticket_id"])["context_snapshot_id"],
        )


def test_verdict_returns_a_materialized_evidence_bundle(
    service, actors, seeded, tmp_path
) -> None:
    ticket, candidate, qa_claim = _candidate_waiting_for_qa(
        service, actors, seeded, tmp_path
    )
    findings = [
        {
            "criterion": criterion,
            "status": "verified",
            "severity": "none",
            "observation": "Observed on the frozen candidate.",
            "evidence": [
                {
                    "type": "test_result",
                    "observation": "Passed.",
                    "storage_ref": f"evidence://qa/result-{index}.json",
                }
            ],
        }
        for index, criterion in enumerate(ticket["acceptance_criteria"], start=1)
    ]

    result = service.submit_qa_verdict(
        actors["qa"], ticket["ticket_id"], qa_claim["lease"]["lease_id"],
        candidate["candidate"]["candidate_id"], qa_claim["ticket"]["version"],
        "passed", findings,
        context_snapshot_id=service.get_ticket_context(actors["qa"], ticket["ticket_id"])["context_snapshot_id"],
    )

    assert len(result["evidence_bundle"]["verified_records"]) == len(findings)
    assert result["evidence_bundle"]["gap_records"] == []
    assert result["evidence_bundle"]["planner_handoff"]["preservation_constraints"]
