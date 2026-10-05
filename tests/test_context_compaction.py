from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from hoh_control_plane.domain import Actor, AuthorizationError, Role, ValidationError
from hoh_control_plane.service import MAX_TOKEN_BUDGET, MIN_TOKEN_BUDGET


# Below the protected floor: forces every optional section out of the packet.
TIGHT_BUDGET = 400
# Above the protected floor: compaction can actually reach this budget.
FIT_BUDGET = 2_500


def _git(path: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(path), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _repository(tmp_path: Path) -> Path:
    repository = tmp_path / "checkout-repository"
    repository.mkdir(parents=True)
    _git(repository, "init", "--initial-branch=main")
    _git(repository, "config", "user.email", "hoh-tests@example.invalid")
    _git(repository, "config", "user.name", "HoH Tests")
    (repository / "README.md").write_text("# Checkout\n", encoding="utf-8")
    _git(repository, "add", "README.md")
    _git(repository, "commit", "-m", "Initial checkout")
    return repository


def _version(service, actor, ticket_id: str) -> int:
    return service.get_ticket_context(actor, ticket_id)["ticket"]["version"]


def _rich_ticket(service, actors, seeded, tmp_path: Path, *, checkpoints: int = 12):
    """A ticket carrying a candidate, QA findings, a worktree and a verbose history."""
    repository = _repository(tmp_path)
    service.configure_repository(actors["planner"], "project-1", str(repository))
    ticket = seeded["ticket"]
    claim = service.claim_ticket(
        actors["developer"], ticket["ticket_id"], ticket["version"], 900
    )
    lease_id = claim["lease"]["lease_id"]
    prepared = service.prepare_worktree(
        actors["developer"], ticket["ticket_id"], lease_id, claim["ticket"]["version"]
    )
    worktree = Path(prepared["worktree"]["worktree_path"])
    retry = worktree / "src" / "checkout" / "retry.txt"
    retry.parent.mkdir(parents=True)
    retry.write_text("safe retry\n", encoding="utf-8")
    _git(worktree, "add", "src/checkout/retry.txt")
    _git(worktree, "commit", "-m", "Implement safe retry")

    for index in range(checkpoints):
        service.append_progress(
            actors["developer"],
            ticket["ticket_id"],
            lease_id,
            f"Checkpoint {index}: "
            + "verbose developer narration that a bounded packet should compact. " * 12,
        )
    decision = service.append_planner_context(
        actors["planner"],
        entry_type="design_decision",
        content=(
            "Durable decision: a retry must stay idempotent for the same order id. " * 12
        ),
        ticket_id=ticket["ticket_id"],
    )

    candidate = service.submit_candidate(
        actors["developer"],
        ticket["ticket_id"],
        lease_id,
        _version(service, actors["developer"], ticket["ticket_id"]),
        _git(worktree, "rev-parse", "HEAD"),
        prepared["worktree"]["base_commit"],
        prepared["worktree"]["branch_ref"],
        ["Retry implementation is ready for QA."],
        context_snapshot_id=service.get_ticket_context(actors["developer"], ticket["ticket_id"])["context_snapshot_id"],
    )
    qa_claim = service.claim_qa(
        actors["qa"], ticket["ticket_id"], candidate["ticket"]["version"], 900
    )
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
                "observation": "The retry still duplicates an order under load.",
                "evidence": [
                    {
                        "type": "test_result",
                        "observation": "duplicate order observed",
                        "storage_ref": "evidence://qa/duplicate-order.json",
                    }
                ],
            }
        ],
        context_snapshot_id=service.get_ticket_context(actors["qa"], ticket["ticket_id"])["context_snapshot_id"],
    )
    return ticket, candidate, decision


def _all_roles(actors) -> dict[str, Actor]:
    return {
        "developer": actors["developer"],
        "qa": actors["qa"],
        "planner": actors["planner"],
        "integrator": actors["integrator"],
    }


def test_every_role_receives_the_bounded_context_envelope(
    service, actors, seeded, tmp_path: Path
) -> None:
    ticket, _candidate, _decision = _rich_ticket(service, actors, seeded, tmp_path)

    for actor in _all_roles(actors).values():
        context = service.get_ticket_context(actor, ticket["ticket_id"])

        assert context["context_snapshot_id"].startswith("ctxsnap_")
        assert context["generated_at"].endswith("Z")
        assert context["context_version"] == context["ticket"]["version"]
        assert context["token_budget"] > 0
        assert isinstance(context["omitted_sections"], list)
        assert context["role"] == actor.role.value


def test_context_snapshot_id_is_deterministic_and_role_specific(
    service, actors, seeded, tmp_path: Path
) -> None:
    """The same state and packet must always compile to the same snapshot identity."""
    ticket, _candidate, _decision = _rich_ticket(service, actors, seeded, tmp_path)

    first = service.get_ticket_context(
        actors["developer"], ticket["ticket_id"], token_budget=TIGHT_BUDGET
    )
    second = service.get_ticket_context(
        actors["developer"], ticket["ticket_id"], token_budget=TIGHT_BUDGET
    )
    qa_packet = service.get_ticket_context(
        actors["qa"], ticket["ticket_id"], token_budget=TIGHT_BUDGET
    )

    assert first["context_snapshot_id"] == second["context_snapshot_id"]
    assert first["omitted_sections"] == second["omitted_sections"]
    assert first["context_snapshot_id"] != qa_packet["context_snapshot_id"]


def test_compaction_retains_criteria_constraints_candidate_sha_and_findings(
    service, actors, seeded, tmp_path: Path
) -> None:
    """Over budget, the safety-critical fields survive for every role."""
    ticket, candidate, _decision = _rich_ticket(service, actors, seeded, tmp_path)

    for actor in _all_roles(actors).values():
        context = service.get_ticket_context(
            actor, ticket["ticket_id"], token_budget=TIGHT_BUDGET
        )

        assert context["ticket"]["acceptance_criteria"] == ticket["acceptance_criteria"]
        assert (
            context["ticket"]["preservation_constraints"]
            == ticket["preservation_constraints"]
        )
        assert context["candidate"]["commit_sha"] == candidate["candidate"]["commit_sha"]
        assert context["known_failures"][0]["status"] == "failed"
        assert (
            context["known_failures"][0]["evidence"][0]["storage_ref"]
            == "evidence://qa/duplicate-order.json"
        )


def test_packet_fits_a_budget_above_the_protected_floor(
    service, actors, seeded, tmp_path: Path
) -> None:
    # Role projection (0.3.18) keeps only the current entry of each durable type,
    # so a default packet already sits near its protected floor. Planner's
    # deliberate history view is what still outgrows a budget, so that is the
    # packet compaction must fit; every role must fit the same budget too.
    ticket, _candidate, _decision = _rich_ticket(service, actors, seeded, tmp_path)
    for index in range(24):
        service.append_planner_context(
            actors["planner"],
            entry_type="planner_decision",
            content=f"Decision {index}: " + "bounded planning state " * 30,
            ticket_id=ticket["ticket_id"],
        )

    full = service.get_ticket_context(
        actors["planner"], ticket["ticket_id"],
        token_budget=MAX_TOKEN_BUDGET, include_history=True,
    )
    bounded = service.get_ticket_context(
        actors["planner"], ticket["ticket_id"],
        token_budget=FIT_BUDGET, include_history=True,
    )
    assert full["estimated_tokens"] > FIT_BUDGET
    assert bounded["estimated_tokens"] <= FIT_BUDGET
    assert bounded["omitted_sections"]

    for actor in _all_roles(actors).values():
        bounded = service.get_ticket_context(
            actor, ticket["ticket_id"], token_budget=FIT_BUDGET
        )
        assert bounded["estimated_tokens"] <= FIT_BUDGET


def test_role_critical_sections_survive_a_budget_below_the_protected_floor(
    service, actors, seeded, tmp_path: Path
) -> None:
    """A budget smaller than the safety floor shrinks the packet, never the floor."""
    ticket, _candidate, _decision = _rich_ticket(service, actors, seeded, tmp_path)

    developer = service.get_ticket_context(
        actors["developer"], ticket["ticket_id"], token_budget=MIN_TOKEN_BUDGET
    )
    qa = service.get_ticket_context(
        actors["qa"], ticket["ticket_id"], token_budget=MIN_TOKEN_BUDGET
    )
    planner = service.get_ticket_context(
        actors["planner"], ticket["ticket_id"], token_budget=MIN_TOKEN_BUDGET
    )

    assert developer["worktree"]["branch_ref"].startswith("hoh/")
    assert developer["worktree"]["base_commit"]
    assert qa["worktree"]["worktree_path"]
    assert planner["feature"]["feature_id"] == ticket["feature_id"]
    for context in (developer, qa, planner):
        assert context["ticket"]["acceptance_criteria"]
        assert context["candidate"]["commit_sha"]


def test_developer_packet_keeps_workspace_identity_before_low_priority_sections(
    service, actors, seeded, tmp_path: Path
) -> None:
    ticket, _candidate, _decision = _rich_ticket(service, actors, seeded, tmp_path)

    context = service.get_ticket_context(
        actors["developer"], ticket["ticket_id"], token_budget=TIGHT_BUDGET
    )

    assert context["worktree"]["branch_ref"].startswith("hoh/")
    assert context["worktree"]["base_commit"]
    omitted = {entry["section"] for entry in context["omitted_sections"]}
    assert "worktree" not in omitted
    assert omitted


def test_qa_packet_keeps_candidate_identity_and_test_interface(
    service, actors, seeded, tmp_path: Path
) -> None:
    ticket, candidate, _decision = _rich_ticket(service, actors, seeded, tmp_path)

    context = service.get_ticket_context(
        actors["qa"], ticket["ticket_id"], token_budget=TIGHT_BUDGET
    )

    assert context["candidate"]["candidate_id"] == candidate["candidate"]["candidate_id"]
    assert context["candidate"]["base_commit"] == candidate["candidate"]["base_commit"]
    assert context["candidate"]["branch_ref"] == candidate["candidate"]["branch_ref"]
    omitted = {entry["section"] for entry in context["omitted_sections"]}
    assert "candidate" not in omitted


def test_verbose_history_becomes_a_checkpoint_with_typed_references(
    service, actors, seeded, tmp_path: Path
) -> None:
    """Nothing is silently deleted: dropped history is still addressable.

    Since 0.3.18 a role packet carries the current entry of each type its role
    acts on; narration and older history leave the packet as one counted,
    resolvable history reference instead of a list of entry references.
    """
    ticket, _candidate, _decision = _rich_ticket(service, actors, seeded, tmp_path)

    for budget in (None, TIGHT_BUDGET):
        packet = service.get_ticket_context(
            actors["developer"], ticket["ticket_id"], token_budget=budget
        )
        assert not [
            entry for entry in packet["context_entries"]
            if entry["entry_type"] == "progress_update"
        ]
        history = [
            entry for entry in packet["omitted_sections"]
            if entry["section"] == "context_entries.history"
        ]
        assert len(history) == 1
        assert history[0]["reason"] == "role_current_view"
        assert history[0]["omitted_count"] >= 12
        assert history[0]["references"] == [packet["context_ref"] + ":history"]

    resolved = service.resolve_context_reference(
        actors["planner"], history[0]["references"][0]
    )
    assert resolved["entries"]
    assert all(entry["entry_id"] for entry in resolved["entries"])


def test_durable_decisions_are_checkpointed_with_a_resolvable_reference(
    service, actors, seeded, tmp_path: Path
) -> None:
    ticket, _candidate, decision = _rich_ticket(service, actors, seeded, tmp_path)

    context = service.get_ticket_context(
        actors["developer"], ticket["ticket_id"], token_budget=TIGHT_BUDGET
    )

    retained = {entry["entry_id"]: entry for entry in context["context_entries"]}
    assert decision["entry_id"] in retained
    assert retained[decision["entry_id"]]["content"] == decision["content"][:200] + "..."
    compacted = [
        item for item in context["omitted_sections"]
        if item["section"] == "context_entries.content"
    ]
    assert f"ctx:entry:{decision['entry_id']}" in compacted[0]["references"]


def test_old_typed_decisions_become_references_but_the_latest_checkpoint_survives(
    service, actors, seeded, tmp_path: Path
) -> None:
    ticket, _candidate, _decision = _rich_ticket(service, actors, seeded, tmp_path)
    decisions = [
        service.append_planner_context(
            actors["planner"],
            entry_type="planner_decision",
            content=f"Decision {index}: " + "bounded planning state " * 30,
            ticket_id=ticket["ticket_id"],
        )
        for index in range(12)
    ]

    # The default Planner packet carries only the newest decision of each type;
    # older decisions are the deliberate history view (0.3.18).
    current = service.get_ticket_context(actors["planner"], ticket["ticket_id"])
    current_ids = {entry["entry_id"] for entry in current["context_entries"]}
    assert decisions[-1]["entry_id"] in current_ids
    assert not current_ids & {decision["entry_id"] for decision in decisions[:-1]}

    packet = service.get_ticket_context(
        actors["planner"], ticket["ticket_id"], token_budget=2_000, include_history=True
    )

    retained = {entry["entry_id"] for entry in packet["context_entries"]}
    omitted = {
        reference.removeprefix("ctx:entry:")
        for section in packet["omitted_sections"]
        if section["section"] == "context_entries"
        for reference in section["references"]
    }
    assert decisions[-1]["entry_id"] in retained
    historical = {decision["entry_id"] for decision in decisions[:-1]}
    assert omitted & historical
    assert historical <= retained | omitted
    assert packet["estimated_tokens"] <= packet["token_budget"]


def test_compacted_history_preserves_actionable_identity_and_reference(
    service, actors, seeded, tmp_path: Path
) -> None:
    """A checkpoint keeps typed identity; its reference resolves full provenance."""
    ticket, _candidate, _decision = _rich_ticket(service, actors, seeded, tmp_path)

    full = service.get_ticket_context(actors["developer"], ticket["ticket_id"])
    bounded = service.get_ticket_context(
        actors["developer"], ticket["ticket_id"], token_budget=TIGHT_BUDGET
    )
    original = {entry["entry_id"]: entry for entry in full["context_entries"]}

    for entry in bounded["context_entries"]:
        source = original[entry["entry_id"]]
        assert entry["entry_type"] == source["entry_type"]
        assert entry["source"] == source["source"]
        assert entry["source_ref"] == source["source_ref"]
        assert entry["artifact_commit"] == source["artifact_commit"]
        assert source["content"].startswith(entry["content"].removesuffix("..."))


def test_token_budget_outside_the_supported_range_is_rejected(
    service, actors, seeded, tmp_path: Path
) -> None:
    ticket, _candidate, _decision = _rich_ticket(service, actors, seeded, tmp_path)

    with pytest.raises(ValidationError, match="budget"):
        service.get_ticket_context(actors["developer"], ticket["ticket_id"], token_budget=1)


def test_bounded_context_still_enforces_cross_project_authorization(
    service, actors, seeded, tmp_path: Path
) -> None:
    ticket, _candidate, _decision = _rich_ticket(service, actors, seeded, tmp_path)
    outsider = Actor("dev-99", Role.DEVELOPER, frozenset({"project-2"}))

    with pytest.raises(AuthorizationError):
        service.get_ticket_context(outsider, ticket["ticket_id"], token_budget=TIGHT_BUDGET)


def test_feature_context_exposes_the_same_bounded_envelope(
    service, actors, seeded, tmp_path: Path
) -> None:
    _ticket, _candidate, _decision = _rich_ticket(service, actors, seeded, tmp_path)

    context = service.get_feature_context(
        actors["planner"], "payment-retry", token_budget=TIGHT_BUDGET
    )

    assert context["context_snapshot_id"].startswith("ctxsnap_")
    assert context["generated_at"].endswith("Z")
    assert context["context_version"] == context["feature"]["version"]
    assert context["token_budget"] == TIGHT_BUDGET
    assert isinstance(context["omitted_sections"], list)
    assert context["estimated_tokens"] <= TIGHT_BUDGET


# A budget large enough that nothing is dropped for size: these tests pin what
# each role's packet contains by contract, not what survives compaction.
WHOLE_PACKET = 60_000


def test_developer_packet_keeps_everything_section_7_2_makes_its_own(
    service, actors, seeded, tmp_path: Path
) -> None:
    """Trimming the packet must never cost a Developer something it acts on."""
    ticket, _candidate, _decision = _rich_ticket(service, actors, seeded, tmp_path)

    packet = service.get_ticket_context(
        actors["developer"], ticket["ticket_id"], WHOLE_PACKET
    )

    # 1. Objective and acceptance criteria. 4. Preservation constraints.
    # 6/8. Related files and the scope limit.
    assert packet["ticket"]["objective"]
    assert packet["ticket"]["acceptance_criteria"]
    assert packet["ticket"]["preservation_constraints"]
    assert packet["ticket"]["file_scope"]
    # 2. Base commit and workspace identity.
    assert packet["worktree"]["worktree_path"]
    assert packet["worktree"]["branch_ref"]
    assert packet["worktree"]["base_commit"]
    # 3. Known failure with its reproduction evidence.
    failure = packet["known_failures"][0]
    assert failure["criterion"] and failure["observation"]
    assert failure["evidence"][0]["storage_ref"]
    # 5. Dependencies. 7. A decision that still stands.
    assert isinstance(packet["dependencies"], list)
    assert any(
        entry["entry_type"] == "design_decision" for entry in packet["context_entries"]
    )
    # Artifact identity is never traded away for size.
    assert packet["candidate"]["commit_sha"]
    assert packet["candidate"]["base_commit"]


def test_qa_packet_keeps_everything_section_7_3_makes_its_own(
    service, actors, seeded, tmp_path: Path
) -> None:
    ticket, candidate, _decision = _rich_ticket(service, actors, seeded, tmp_path)

    packet = service.get_ticket_context(actors["qa"], ticket["ticket_id"], WHOLE_PACKET)

    # 1. Immutable candidate identity.
    assert packet["candidate"]["candidate_id"] == candidate["candidate"]["candidate_id"]
    assert packet["candidate"]["commit_sha"]
    assert packet["candidate"]["base_commit"]
    assert packet["candidate"]["branch_ref"]
    # 2. Requirement and acceptance criteria. 3. Preservation constraints.
    assert packet["ticket"]["objective"]
    assert packet["ticket"]["acceptance_criteria"]
    assert packet["ticket"]["preservation_constraints"]
    # 4. Test environment and the interface QA executes against.
    assert packet["worktree"]["worktree_path"]
    assert packet["worktree"]["base_commit"]
    # 5. Known risks carried forward, with the provenance QA judges them by.
    evidence = packet["known_failures"][0]["evidence"][0]
    assert evidence["storage_ref"]
    assert "environment_digest" in evidence and "storage_checksum" in evidence
    # 6. Developer evidence, kept for reference rather than trusted: QA gets
    # only the reference, so it observes before it reads the maker's claims.
    claims_ref = f"ctx:candidate:{candidate['candidate']['candidate_id']}:developer_claims"
    assert packet["candidate"]["developer_claims"] == []
    assert any(
        claims_ref in item["references"] for item in packet["omitted_sections"]
    )
    resolved = service.resolve_context_reference(actors["qa"], claims_ref)
    assert resolved["developer_claims"] == ["Retry implementation is ready for QA."]


def test_packets_drop_repeated_identity_and_machine_local_paths(
    service, actors, seeded, tmp_path: Path
) -> None:
    """What the envelope already says, or what no other machine can resolve."""
    ticket, _candidate, _decision = _rich_ticket(service, actors, seeded, tmp_path)

    for actor in _all_roles(actors).values():
        packet = service.get_ticket_context(actor, ticket["ticket_id"], WHOLE_PACKET)

        for entry in packet["context_entries"]:
            assert "ticket_id" not in entry
            assert "project_id" not in entry
            assert "superseded_by" not in entry
            assert "actor_id" not in entry
            assert "created_at" not in entry
        if packet.get("candidate"):
            assert "ticket_id" not in packet["candidate"]
        if packet.get("worktree"):
            assert "ticket_id" not in packet["worktree"]
        for run in packet.get("test_runs") or []:
            # An absolute path on the runner's machine is unusable to a reader;
            # storage_ref is the portable reference to the same report.
            assert "report_path" not in run
            assert run["storage_ref"]
        # Identity the envelope carries is not repeated per row.
        assert packet["ticket"]["ticket_id"] == ticket["ticket_id"]


def test_an_execution_role_gets_feature_intent_and_a_planner_gets_the_whole_feature(
    service, actors, seeded, tmp_path: Path
) -> None:
    ticket, _candidate, _decision = _rich_ticket(service, actors, seeded, tmp_path)

    developer = service.get_ticket_context(
        actors["developer"], ticket["ticket_id"], WHOLE_PACKET
    )
    planner = service.get_ticket_context(
        actors["planner"], ticket["ticket_id"], WHOLE_PACKET
    )

    assert developer["feature"]["objective"], "intent is why the ticket exists"
    assert developer["feature"]["title"]
    assert "created_at" not in developer["feature"]
    assert "priority" not in developer["feature"]
    # The Planner plans the feature, so it keeps the planning fields.
    assert "priority" in planner["feature"]
    assert "parent_feature_id" in planner["feature"]
    # Timestamps are bookkeeping for every role (0.3.18 packet projection).
    assert "created_at" not in planner["feature"]


def test_a_developer_gets_test_outcomes_without_qa_provenance_machinery(
    service, actors
) -> None:
    """Asserted on the projection itself so an empty run list cannot pass it vacuously."""
    run = {
        "test_run_id": "testrun_1", "candidate_id": "cand_1", "ticket_id": "DEV-142",
        "command_digest": "digest", "exit_code": 1, "timed_out": 0, "duration_ms": 90,
        "storage_ref": "evidence://local/report.json",
        "report_path": "D:\\HoH\\data\\evidence\\report.json",
        "environment_digest": "env", "storage_checksum": "sum",
        "executed_by": "qa-01", "created_at": "2026-09-11T00:00:00Z",
    }

    packets = {}
    for name in ("developer", "qa"):
        payload = {"feature": {}, "test_runs": [dict(run)], "context_entries": []}
        service._project_role_sections(payload, actors[name], include_history=False)
        packets[name] = payload["test_runs"][0]

    assert packets["developer"]["exit_code"] == 1, "a Developer still sees the outcome"
    assert packets["developer"]["storage_ref"], "and can still resolve the full report"
    assert "command_digest" not in packets["developer"]
    assert "storage_checksum" not in packets["developer"]
    assert packets["qa"]["command_digest"], "QA proves which command produced a run"
    assert packets["qa"]["environment_digest"] and packets["qa"]["storage_checksum"]
    # The machine-local path is unusable to any reader, so no role receives it.
    assert "report_path" not in packets["developer"]
    assert "report_path" not in packets["qa"]


def test_trimming_leaves_the_packet_smaller_than_before(
    service, actors, seeded, tmp_path: Path
) -> None:
    """The point of the projection: budget is spent on material, not bookkeeping."""
    ticket, _candidate, _decision = _rich_ticket(service, actors, seeded, tmp_path)

    packet = service.get_ticket_context(
        actors["developer"], ticket["ticket_id"], WHOLE_PACKET
    )

    rendered = json.dumps(packet)
    assert '"superseded_by"' not in rendered
    assert '"report_path"' not in rendered
    # Every context entry used to repeat these three; none should now.
    assert rendered.count('"project_id"') <= 1
