"""HOH-278: the QA capability approval cycle can no longer deadlock.

The reported cycle: QA needed a lease to produce preflight evidence, a lease
needed an approved capability, and approval needed independent evidence. No
tool could start it, so QA and Planner both went to wait_for_event. On the live
database no QA agent in any project had ever been approved for anything.

Evidence now comes from the HoH host, before any lease, bound to the QA agent
and a fingerprint of its machine. Approval accepts only that evidence, and a
candidate blocked on approval alone produces exactly one Planner job that names
the call which unblocks it.
"""
from __future__ import annotations

import asyncio

import pytest

from conftest import FakeProber
from hoh_control_plane.domain import Actor, Role, TicketStatus, ValidationError

CAPABILITIES = ["browser_interaction", "build_execution"]
HOST = "qa-host-a"



@pytest.fixture
def blocked(service, actors, seeded):
    """The reported state: a QA-ready candidate, a QA that declared the right
    equipment, and no approval anyone can justify."""
    prober = FakeProber()
    service.capability_prober = prober
    ticket = seeded["ticket"]
    contract = [
        {
            "criterion": criterion,
            "scenario_ref": f"browser:{index}",
            "method": "black_box",
            "expected_observation": f"Observed in a browser: {criterion}",
            "required_evidence_types": ["test_result"],
            "required_capabilities": CAPABILITIES,
        }
        for index, criterion in enumerate(ticket["acceptance_criteria"], start=1)
    ]
    service.configure_validation_contract(
        actors["planner"], ticket["ticket_id"], ticket["version"], contract, "Browser surface.",
    )
    service.configure_verification_profile(
        actors["planner"], "project-1", capabilities=CAPABILITIES + ["runtime_trace"],
        quality_policy="claim-evidence-v1",
    )
    version = service.get_ticket_context(actors["planner"], ticket["ticket_id"])["ticket"]["version"]
    claimed = service.claim_ticket(actors["developer"], ticket["ticket_id"], version, 900)
    submitted = service.submit_candidate(
        actors["developer"], ticket["ticket_id"], claimed["lease"]["lease_id"],
        claimed["ticket"]["version"], "a" * 40, "b" * 40, "agent/dev-01/DEV-142",
        ["Ready for independent QA."],
        context_snapshot_id=service.get_ticket_context(actors["developer"], ticket["ticket_id"])["context_snapshot_id"],
    )
    service.register_agent(
        actors["qa"], skills=["hoh-qa"], execution_capabilities=CAPABILITIES, machine_id=HOST,
    )
    return {"prober": prober, "ticket_id": ticket["ticket_id"], "submitted": submitted}


def _open_jobs(service, ticket_id: str) -> list[dict]:
    with service.store.connection() as connection:
        return [dict(row) for row in connection.execute(
            "SELECT * FROM planner_jobs WHERE ticket_id = ? AND status IN ('READY','IN_PROGRESS')",
            (ticket_id,),
        )]


def _leases(service, ticket_id: str) -> int:
    with service.store.connection() as connection:
        return connection.execute(
            "SELECT COUNT(*) FROM leases WHERE ticket_id = ?", (ticket_id,)
        ).fetchone()[0]


# ---------------------------------------------------------------------------
# The cycle, reproduced and broken.


def test_the_reported_cycle_is_real_before_any_preflight(service, actors, blocked) -> None:
    inbox = service.get_agent_inbox(actors["qa"], "project-1")

    assert inbox["offers"] == []
    [blocker] = inbox["capability_blockers"]
    assert blocker["missing_approval"] == CAPABILITIES
    assert blocker["missing_declaration"] == []
    # QA is told the one lease-free action that starts the loop.
    assert "qa.run_capability_preflight" in blocker["next_action"]
    assert inbox["capability_preflight"] is None
    with pytest.raises(ValidationError):
        service.claim_qa(actors["qa"], blocked["ticket_id"],
                         blocked["submitted"]["ticket"]["version"], 900)


def test_qa_publishes_preflight_evidence_without_any_lease(service, actors, blocked) -> None:
    assert _leases(service, blocked["ticket_id"]) == 0

    result = service.run_capability_preflight(actors["qa"], "project-1")

    assert result["preflight_ref"].startswith("preflight:")
    assert result["proven"] == CAPABILITIES
    assert result["host_matches_machine"] is True
    assert _leases(service, blocked["ticket_id"]) == 0, "no lease was needed or taken"
    with service.store.connection() as connection:
        record = dict(connection.execute(
            "SELECT * FROM capability_preflights WHERE preflight_id = ?",
            (result["preflight_ref"].removeprefix("preflight:"),),
        ).fetchone())
    assert record["agent_id"] == actors["qa"].agent_id
    assert HOST not in str(record), "the machine is kept as a fingerprint, never its name"


def test_the_preflight_approves_and_qa_can_claim_at_once(service, actors, blocked) -> None:
    """Wave 7A: the server approves declared and proven itself; no Planner turn."""
    result = service.run_capability_preflight(actors["qa"], "project-1")

    assert result["approved"] == CAPABILITIES
    assert _open_jobs(service, blocked["ticket_id"]) == []
    with service.store.connection() as connection:
        presence = dict(connection.execute(
            "SELECT * FROM agent_presence WHERE agent_id = ?", (actors["qa"].agent_id,)
        ).fetchone())
    assert presence["capability_approved_by"] == "system:host-preflight"
    claim = service.claim_qa(actors["qa"], blocked["ticket_id"],
                             blocked["submitted"]["ticket"]["version"], 900)
    assert claim["lease"]["role"] == Role.QA.value


def test_a_preflight_closes_a_review_job_opened_before_it(service, actors, blocked) -> None:
    blocked["prober"].equipped = {"build_execution"}
    service.run_capability_preflight(actors["qa"], "project-1")
    [job] = _open_jobs(service, blocked["ticket_id"])
    assert "owner=infrastructure" in job["summary"]

    blocked["prober"].equipped = set(CAPABILITIES)
    result = service.run_capability_preflight(actors["qa"], "project-1")

    assert result["resolved_planner_jobs"] == [job["planner_job_id"]]
    assert _open_jobs(service, blocked["ticket_id"]) == []


def test_the_approval_persists_until_the_configuration_changes(
    service, actors, blocked
) -> None:
    service.run_capability_preflight(actors["qa"], "project-1")
    version = blocked["submitted"]["ticket"]["version"]

    # A new session on the same machine with the same declaration keeps it.
    service.register_agent(
        actors["qa"], skills=["hoh-qa"], execution_capabilities=CAPABILITIES, machine_id=HOST,
    )
    assert service.get_agent_inbox(actors["qa"], "project-1")["capability_blockers"] == []

    # The project's preflight configuration changes: the approval lapses, and
    # the earlier record is not reused for it.
    service.configure_capability_preflight(
        actors["planner"], "project-1", "build_execution", ["node", "--version"], "Node build.",
    )
    inbox = service.get_agent_inbox(actors["qa"], "project-1")
    assert inbox["capability_blockers"][0]["missing_approval"] == CAPABILITIES
    assert inbox["capability_preflight"] is None
    with pytest.raises(ValidationError):
        service.claim_qa(actors["qa"], blocked["ticket_id"], version, 900)

    assert service.run_capability_preflight(actors["qa"], "project-1")["approved"] == CAPABILITIES
    service.claim_qa(actors["qa"], blocked["ticket_id"], version, 900)


def test_a_changed_declaration_revokes_the_server_approval(service, actors, blocked) -> None:
    service.run_capability_preflight(actors["qa"], "project-1")

    service.register_agent(
        actors["qa"], skills=["hoh-qa"], execution_capabilities=["build_execution"],
        machine_id=HOST,
    )

    with service.store.connection() as connection:
        approved = connection.execute(
            "SELECT approved_execution_capabilities FROM agent_presence WHERE agent_id = ?",
            (actors["qa"].agent_id,),
        ).fetchone()[0]
    assert approved == "[]"


def test_an_unproven_capability_is_never_approved(service, actors, blocked) -> None:
    blocked["prober"].equipped = {"build_execution"}

    result = service.run_capability_preflight(actors["qa"], "project-1")

    assert result["approved"] == ["build_execution"]
    with pytest.raises(ValidationError):
        service.claim_qa(actors["qa"], blocked["ticket_id"],
                         blocked["submitted"]["ticket"]["version"], 900)


def test_a_preflight_on_another_host_approves_nothing(service, actors, blocked) -> None:
    blocked["prober"].host = "the-hoh-host"

    result = service.run_capability_preflight(actors["qa"], "project-1")

    assert result["approved"] is None
    with pytest.raises(ValidationError):
        service.claim_qa(actors["qa"], blocked["ticket_id"],
                         blocked["submitted"]["ticket"]["version"], 900)


def _clear_approval(service, agent_id: str) -> None:
    """The state of an agent nobody approved, as before wave 7A."""
    with service.store.connection(immediate=True) as connection:
        connection.execute(
            "UPDATE agent_presence SET approved_execution_capabilities = '[]', "
            "capability_approved_by = NULL WHERE agent_id = ?",
            (agent_id,),
        )


# ---------------------------------------------------------------------------
# What the Planner sees, and what approval accepts.


def test_the_planner_review_event_carries_evidence_and_no_machine_name(
    service, actors, blocked
) -> None:
    from hoh_control_plane.event_wait import _pending

    service.run_capability_preflight(actors["qa"], "project-1")
    _clear_approval(service, actors["qa"].agent_id)
    service.register_agent(actors["planner"], skills=["hoh-planner"], capacity=1)
    # Resolve the job so the review event, not the job, is what the Planner sees.
    with service.store.connection(immediate=True) as connection:
        connection.execute("UPDATE planner_jobs SET status = 'RESOLVED'")

    [(_key, event)] = _pending(service, actors["planner"], "project-1")

    assert event["kind"] == "capability_review_required"
    assert event["declared_capabilities"] == CAPABILITIES
    assert event["evidence_refs"][0].startswith("preflight:")
    assert event["proven_capabilities"] == CAPABILITIES
    assert "machine_id" not in event
    assert HOST not in str(event)
    assert "planner.configure_agent_capability_profile" in event["next_action"]


def test_a_planner_override_is_still_accepted_from_the_preflight(
    service, actors, blocked
) -> None:
    result = service.run_capability_preflight(actors["qa"], "project-1")
    _clear_approval(service, actors["qa"].agent_id)

    approved = service.configure_agent_capability_profile(
        actors["planner"], "project-1", actors["qa"].agent_id, ["build_execution"],
        [result["preflight_ref"]],
    )

    assert approved["approved_execution_capabilities"] == ["build_execution"]
    assert approved["capability_approved_by"] == actors["planner"].agent_id


@pytest.mark.parametrize(
    ("evidence", "capabilities", "reason"),
    [
        (["evidence://qa-01/my-own-report"], CAPABILITIES, "evidence_not_preflight"),
        (["preflight:preflight_9999"], CAPABILITIES, "evidence_unknown"),
        (None, ["browser_interaction", "runtime_trace"], "capability_not_declared"),
    ],
)
def test_approval_fails_closed_on_anything_but_matching_host_evidence(
    service, actors, blocked, evidence, capabilities, reason
) -> None:
    result = service.run_capability_preflight(actors["qa"], "project-1")

    with pytest.raises(ValidationError) as error:
        service.configure_agent_capability_profile(
            actors["planner"], "project-1", actors["qa"].agent_id, capabilities,
            evidence or [result["preflight_ref"]],
        )

    assert error.value.details["reason"] == reason


def test_evidence_from_another_machine_or_agent_is_refused(service, actors, blocked) -> None:
    result = service.run_capability_preflight(actors["qa"], "project-1")

    # The same agent re-registers from another machine: its old evidence no
    # longer describes where it runs.
    service.register_agent(
        actors["qa"], skills=["hoh-qa"], execution_capabilities=CAPABILITIES,
        machine_id="qa-host-b",
    )
    with pytest.raises(ValidationError) as other_machine:
        service.configure_agent_capability_profile(
            actors["planner"], "project-1", actors["qa"].agent_id, CAPABILITIES,
            [result["preflight_ref"]],
        )
    assert other_machine.value.details["reason"] == "evidence_other_machine"

    qa_two = Actor("qa-02", Role.QA, frozenset({"project-1"}))
    service.register_agent(
        qa_two, skills=["hoh-qa"], execution_capabilities=CAPABILITIES, machine_id=HOST,
    )
    with pytest.raises(ValidationError) as other_agent:
        service.configure_agent_capability_profile(
            actors["planner"], "project-1", qa_two.agent_id, CAPABILITIES,
            [result["preflight_ref"]],
        )
    assert other_agent.value.details["reason"] == "evidence_other_agent"


def test_only_the_proven_subset_can_be_approved(service, actors, blocked) -> None:
    blocked["prober"].equipped = {"build_execution"}
    result = service.run_capability_preflight(actors["qa"], "project-1")

    assert result["unproven"] == {"browser_interaction": "no_preflight_defined"}
    with pytest.raises(ValidationError) as error:
        service.configure_agent_capability_profile(
            actors["planner"], "project-1", actors["qa"].agent_id, CAPABILITIES,
            [result["preflight_ref"]],
        )
    assert error.value.details == {
        "reason": "capability_not_proven", "capabilities": ["browser_interaction"],
    }
    # And the job says so, routed to infrastructure rather than to an approval.
    [job] = _open_jobs(service, blocked["ticket_id"])
    assert "owner=infrastructure action=capability_preflight_gap" in job["summary"]
    assert "browser_interaction" in job["summary"]


def test_a_qa_on_another_machine_is_routed_to_infrastructure(service, actors, blocked) -> None:
    blocked["prober"].host = "the-hoh-host"

    result = service.run_capability_preflight(actors["qa"], "project-1")

    assert result["host_matches_machine"] is False
    assert result["proven"] == []
    assert set(result["unproven"].values()) == {"qa_machine_is_not_this_host"}
    [job] = _open_jobs(service, blocked["ticket_id"])
    assert "owner=infrastructure" in job["summary"]


def test_a_configured_preflight_proves_a_project_specific_capability(
    service, actors, blocked
) -> None:
    blocked["prober"].equipped = {"browser_interaction"}
    service.configure_capability_preflight(
        actors["planner"], "project-1", "build_execution", ["node", "--version"],
        "The project builds with node.",
    )

    result = service.run_capability_preflight(actors["qa"], "project-1")

    assert result["proven"] == CAPABILITIES
    with pytest.raises(ValidationError):
        service.configure_capability_preflight(
            actors["planner"], "project-1", "not_in_profile", ["true"], "Outside the profile.",
        )


# ---------------------------------------------------------------------------
# What the fix must not do.


def test_breaking_the_cycle_never_touches_the_candidate_or_invents_a_verdict(
    service, actors, blocked
) -> None:
    before = service.get_ticket_context(actors["planner"], blocked["ticket_id"])
    service.run_capability_preflight(actors["qa"], "project-1")
    service.get_agent_inbox(actors["qa"], "project-1")
    after = service.get_ticket_context(actors["planner"], blocked["ticket_id"])

    assert after["ticket"]["status"] == TicketStatus.QA_READY.value
    assert after["candidate"]["commit_sha"] == before["candidate"]["commit_sha"]
    assert after["candidate"]["candidate_id"] == before["candidate"]["candidate_id"]
    with service.store.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM qa_findings").fetchone()[0] == 0


def test_only_qa_may_run_a_preflight_and_only_the_planner_may_configure_one(
    service, actors, blocked
) -> None:
    from hoh_control_plane.domain import AuthorizationError

    with pytest.raises(AuthorizationError):
        service.run_capability_preflight(actors["developer"], "project-1")
    with pytest.raises(AuthorizationError):
        service.configure_capability_preflight(
            actors["qa"], "project-1", "build_execution", ["true"], "Self-configured.",
        )


@pytest.mark.anyio
async def test_the_tools_are_exposed_to_the_right_roles(service, actors, blocked) -> None:
    from hoh_control_plane.mcp_server import create_mcp_server

    qa_tools = {tool.name for tool in await create_mcp_server(service, actors["qa"]).list_tools()}
    planner_tools = {
        tool.name for tool in await create_mcp_server(service, actors["planner"]).list_tools()
    }

    assert "qa.run_capability_preflight" in qa_tools
    assert "planner.configure_capability_preflight" not in qa_tools
    assert "planner.configure_capability_preflight" in planner_tools
    assert "qa.run_capability_preflight" not in planner_tools

    _content, result = await create_mcp_server(service, actors["qa"]).call_tool(
        "qa.run_capability_preflight", {"project_id": "project-1"}
    )
    assert result["proven"] == CAPABILITIES
