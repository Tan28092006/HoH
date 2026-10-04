"""The ten things section 25 says must be true of the finished system.

The spec states these as the definition of success for the whole control plane,
and until now nothing checked them. Each test below is named for its criterion so
a failure says which promise broke, rather than which function did.

Two of the ten are not mechanically checkable from here and say so in place,
rather than being quietly dropped or asserted with something weaker that would
read as coverage.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from hoh_control_plane.domain import Actor, AuthorizationError, ConflictError, Role


def _git(path: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(path), *arguments], check=True, capture_output=True, text=True
    )
    return completed.stdout.strip()


def _repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repository"
    repository.mkdir(parents=True)
    _git(repository, "init", "--initial-branch=main")
    _git(repository, "config", "user.email", "hoh-tests@example.invalid")
    _git(repository, "config", "user.name", "HoH Tests")
    (repository / "README.md").write_text("# Checkout\n", encoding="utf-8")
    _git(repository, "add", "README.md")
    _git(repository, "commit", "-m", "Initial checkout")
    return repository


# 1. A newly started AI can find its own work through the Metadata MCP.


def test_criterion_1_a_new_agent_finds_its_own_work_unaided(
    service, actors, seeded
) -> None:
    discovered = service.discover_work(actors["developer"], "project-1")

    offered = [
        entry for feature in discovered["features"] for entry in feature["tickets"]
    ]
    assert offered, "a Developer starting cold is handed work"
    assert all(entry["required_role"] == "developer" for entry in offered)
    assert all(
        entry["recommended_calls"][0]["tool"] == "ticket.get_context" for entry in offered
    ), "and is told how to resolve what it found"


# 2. An agent receives only the context it needs, and knows where each part came from.


def test_criterion_2_context_is_bounded_and_says_where_it_came_from(
    service, actors, seeded
) -> None:
    packet = service.get_ticket_context(
        actors["developer"], seeded["ticket"]["ticket_id"], 4_000
    )

    assert packet["context_snapshot_id"].startswith("ctxsnap_")
    assert packet["context_version"] == packet["ticket"]["version"]
    assert packet["estimated_tokens"] <= 4_000
    assert isinstance(packet["omitted_sections"], list), "what was left out is stated"
    # Bounded by role, not merely by size: a Developer packet is not a QA packet.
    assert packet["role"] == Role.DEVELOPER.value


# 3. No two agents can accidentally write to the same ticket or worktree scope.


def test_criterion_3_two_agents_cannot_take_the_same_ticket(
    service, actors, seeded
) -> None:
    ticket = seeded["ticket"]
    service.claim_ticket(actors["developer"], ticket["ticket_id"], ticket["version"], 900)

    with pytest.raises(ConflictError):
        service.claim_ticket(
            actors["developer_2"], ticket["ticket_id"], ticket["version"], 900
        )


def test_criterion_3_overlapping_write_scope_is_serialised(
    service, actors, seeded
) -> None:
    planner, ticket = actors["planner"], seeded["ticket"]
    competing = service.create_ticket(
        planner,
        feature_id="payment-retry",
        ticket_id="DEV-777",
        title="Another change to the same files",
        objective="Touches what the first ticket is already changing",
        required_role=Role.DEVELOPER,
        acceptance_criteria=["Its own criterion"],
        preservation_constraints=[],
        file_scope=ticket["file_scope"],
        ready=True,
    )
    service.claim_ticket(actors["developer"], ticket["ticket_id"], ticket["version"], 900)

    with pytest.raises(ConflictError):
        service.claim_ticket(
            actors["developer_2"], competing["ticket_id"], competing["version"], 900
        )


# 4. No QA verdict is attached to an unidentified artifact.


def test_criterion_4_a_verdict_names_the_candidate_it_judged(
    service, actors, seeded
) -> None:
    import inspect

    signature = inspect.signature(service.submit_qa_verdict)

    assert "candidate_id" in signature.parameters
    assert signature.parameters["candidate_id"].default is inspect.Parameter.empty, (
        "the artifact under judgement cannot be omitted"
    )


# 5. Unfinished work stays discoverable to the role that can act on it.


def test_criterion_5_unfinished_work_reaches_the_role_that_can_act(
    service, actors, seeded
) -> None:
    ticket = seeded["ticket"]
    claim = service.claim_ticket(
        actors["developer"], ticket["ticket_id"], ticket["version"], 900
    )
    service.report_blocker(
        actors["developer"],
        ticket["ticket_id"],
        claim["lease"]["lease_id"],
        claim["ticket"]["version"],
        "Waiting on a contract decision only a Planner can make.",
    )

    # Blocked work stops being offered to a Developer, which is the point, and
    # becomes visible to the Planner who is now the one who can move it.
    for_developer = service.discover_work(actors["developer"], "project-1")
    for_planner = service.discover_work(actors["planner"], "project-1", blocked_only=True)

    assert ticket["ticket_id"] not in {
        entry["ticket_id"]
        for feature in for_developer["features"]
        for entry in feature["tickets"]
    }
    assert ticket["ticket_id"] in {
        entry["ticket_id"]
        for feature in for_planner["features"]
        for entry in feature["tickets"]
    }


# 6. Verified behaviour becomes a preservation constraint on later changes.


def test_criterion_6_proven_behaviour_constrains_what_comes_next(
    service, actors, seeded
) -> None:
    planner = actors["planner"]
    proven = service.create_ticket(
        planner,
        feature_id="payment-retry",
        ticket_id="DEV-901",
        title="Already proven",
        objective="Something QA verified",
        required_role=Role.DEVELOPER,
        acceptance_criteria=["A refund reaches the card within a day"],
        preservation_constraints=[],
        file_scope=["src/proven/**"],
    )
    with service.store.connection(immediate=True) as connection:
        connection.execute(
            "UPDATE tickets SET status = 'DONE' WHERE ticket_id = ?", (proven["ticket_id"],)
        )
    later = service.create_ticket(
        planner,
        feature_id="payment-retry",
        ticket_id="DEV-902",
        title="What comes next",
        objective="Must not break what was proven",
        required_role=Role.DEVELOPER,
        acceptance_criteria=["Its own criterion"],
        preservation_constraints=[],
        file_scope=["src/next/**"],
    )

    packet = service.get_ticket_context(actors["developer"], later["ticket_id"], 60_000)

    assert [entry["behaviour"] for entry in packet["inherited_constraints"]] == [
        "A refund reaches the card within a day"
    ]


# 7. A person can trace the whole process.


def test_criterion_7_the_whole_process_is_traceable(service, actors, seeded) -> None:
    """The UI renders this; what is checkable here is that the data behind it exists."""
    ticket = seeded["ticket"]
    service.claim_ticket(actors["developer"], ticket["ticket_id"], ticket["version"], 900)

    board = service.board("project-1")
    events = service.list_events(actors["planner"], "project-1")
    activity = service.list_agent_activity(actors["planner"], "project-1")

    assert board["columns"], "work is visible by state"
    assert events["events"], "every transition left a record"
    assert "next_cursor" in events, "and the record can be followed forward"
    assert isinstance(activity, dict)


# 8. Role permissions are enforced by the backend, not by prompt obedience.


def test_criterion_8_the_backend_refuses_what_a_role_may_not_do(
    service, actors, seeded
) -> None:
    """Refused by the service itself, so no amount of persuasion reaches the state.

    Each call is made with arguments that would otherwise be valid, so the refusal
    can only be about who is asking. Catching a TypeError here instead would pass
    on a missing argument and prove nothing.
    """
    ticket = seeded["ticket"]
    refusals = [
        # A Developer cannot judge its own work.
        lambda: service.submit_qa_verdict(
            actors["developer"], ticket["ticket_id"], "lease", "cand", 1, "passed",
            [{"criterion": "c", "status": "verified", "observation": "o",
              "evidence": [{"type": "t", "observation": "o", "storage_ref": "evidence://x"}]}],
        ),
        # QA cannot submit the artifact it is meant to judge.
        lambda: service.submit_candidate(
            actors["qa"], ticket["ticket_id"], "lease", 1, "a" * 40, "b" * 40, "branch", ["claim"],
        ),
        # A Developer cannot move the protected branch.
        lambda: service.merge_candidate(
            actors["developer"], ticket["ticket_id"], "lease", 1,
        ),
        # QA cannot author the work it will verify.
        lambda: service.create_ticket(
            actors["qa"], feature_id="payment-retry", ticket_id="DEV-950",
            title="t", objective="o", required_role=Role.DEVELOPER,
            acceptance_criteria=["c"], preservation_constraints=[], file_scope=["src/**"],
        ),
        # A Developer cannot record work as landed. (A Planner may, but only
        # while reconciling observed Git movement; see test_external_landing.)
        lambda: service.record_external_landing(
            actors["developer"], ticket["ticket_id"], ticket["version"], "a" * 40, "reason",
        ),
    ]

    for attempt in refusals:
        with pytest.raises(AuthorizationError):
            attempt()


def test_criterion_8_a_forged_role_claim_is_not_believed(service, seeded) -> None:
    """Identity is server-side; a client cannot declare itself into another role."""
    from hoh_control_plane.auth import AgentCredential, resolve_actor

    credentials = (
        AgentCredential(
            "a-token-long-enough-to-be-a-real-secret",
            Actor("dev-01", Role.DEVELOPER, frozenset({"project-1"})),
        ),
    )

    actor = resolve_actor(credentials, "Bearer a-token-long-enough-to-be-a-real-secret")

    assert actor.role is Role.DEVELOPER, "the credential decides, not the caller"


# 9. A replacement agent continues from ticket context, with no prior chat.


def test_criterion_9_a_replacement_continues_from_the_ticket_alone(
    service, actors, seeded, tmp_path: Path
) -> None:
    # Since 0.3.18 progress narration is not carried in any role packet; what
    # the previous agent reached travels as its committed checkpoint and a
    # typed worktree_handoff entry, which is what this checks.
    service.configure_repository(actors["planner"], "project-1", str(_repository(tmp_path)))
    ticket = seeded["ticket"]
    first = service.claim_ticket(
        actors["developer"], ticket["ticket_id"], ticket["version"], 30
    )
    worktree = service.prepare_worktree(
        actors["developer"], ticket["ticket_id"], first["lease"]["lease_id"],
        first["ticket"]["version"],
    )["worktree"]
    old_path = Path(worktree["worktree_path"])
    (old_path / "half-finished.txt").write_text("partial work\n", encoding="utf-8")
    _git(old_path, "add", "half-finished.txt")
    _git(old_path, "commit", "-m", "Half-finished work the next agent has to pick up")
    checkpoint = _git(old_path, "rev-parse", "HEAD")
    service.append_progress(
        actors["developer"],
        ticket["ticket_id"],
        first["lease"]["lease_id"],
        "Half-finished work the next agent has to pick up.",
    )
    with service.store.connection(immediate=True) as connection:
        connection.execute(
            "UPDATE leases SET expires_at = ? WHERE ticket_id = ?",
            ("2000-01-01T00:00:00+00:00", ticket["ticket_id"]),
        )
    sweep = service.sweep_expired_leases(Actor("scheduler-01", Role.ADMIN, frozenset({"*"})))
    version = next(
        entry["version"] for entry in sweep["requeued"] if entry["ticket_id"] == ticket["ticket_id"]
    )
    second = service.claim_ticket(actors["developer_2"], ticket["ticket_id"], version, 900)
    replacement = service.prepare_worktree(
        actors["developer_2"], ticket["ticket_id"], second["lease"]["lease_id"],
        second["ticket"]["version"],
    )["worktree"]

    packet = service.get_ticket_context(
        actors["developer_2"], ticket["ticket_id"], 60_000
    )

    assert packet["ticket"]["objective"], "what it is for"
    assert packet["ticket"]["acceptance_criteria"], "what it must satisfy"
    assert packet["ticket"]["file_scope"], "where it may write"
    assert any(
        entry["entry_type"] == "worktree_handoff" and entry["artifact_commit"] == checkpoint
        for entry in packet["context_entries"]
    ), "and what the previous agent had reached, without its chat"
    assert _git(Path(replacement["worktree_path"]), "rev-parse", "HEAD") == checkpoint


# 10. Jira and GitHub can be replaced or switched off without losing workflow state.


def test_criterion_10_no_workflow_state_depends_on_an_external_tracker(
    service, actors, seeded
) -> None:
    source = Path(__file__).resolve().parents[1] / "src/hoh_control_plane"
    mentions = [
        path.name
        for path in source.rglob("*.py")
        if any(word in path.read_text(encoding="utf-8").lower() for word in ("jira", "github"))
    ]

    assert mentions == [], "core workflow state is not coupled to any external tracker"


def test_criterion_10_the_whole_workflow_runs_with_no_integration_configured(
    service, actors, seeded
) -> None:
    ticket = seeded["ticket"]
    claim = service.claim_ticket(
        actors["developer"], ticket["ticket_id"], ticket["version"], 900
    )

    assert claim["ticket"]["status"] == "DEV_IN_PROGRESS", (
        "work proceeds with nothing external configured"
    )
