"""Wave 7C-3 edge cases: computed risk tiers and the merge gate they impose."""
from __future__ import annotations

import pytest
from helpers.w7 import (
    claim_integration,
    events,
    finding,
    queue_merge,
    submit_fake,
    ticket,
    verified_with_repository,
)

from hoh_control_plane.domain import (
    Actor,
    AuthorizationError,
    ConflictError,
    ReleasePolicyError,
    Role,
    ValidationError,
)
from hoh_control_plane.service import ControlPlaneService
from hoh_control_plane.service.risk_policy import (
    DEFAULT_HIGH_RISK_PATTERNS,
    DEFAULT_MEDIUM_MAX_FILES,
    DEFAULT_MEDIUM_RISK_PATTERNS,
)

DEFAULTS = {
    "checker_distinct_from_maker": True,
    "high_risk_patterns": list(DEFAULT_HIGH_RISK_PATTERNS),
    "medium_risk_patterns": list(DEFAULT_MEDIUM_RISK_PATTERNS),
    "medium_max_files": DEFAULT_MEDIUM_MAX_FILES,
}


def _classify(changed, scope=(), **policy):
    service = object.__new__(ControlPlaneService)
    return service._classify_risk({**DEFAULTS, **policy}, changed, list(scope))


# --------------------------------------------------------- classification


@pytest.mark.parametrize(
    "path",
    ["src/hoh_control_plane/store_schema.py", "store_schema.py",
     "src/hoh_control_plane/store_migrations.py", "src/hoh_control_plane/auth.py",
     "a/b/c/auth.py", "src/hoh_control_plane/local_credentials.py", "config/credentials.json",
     "deploy/desktop/install.ps1", "skills/hoh-qa/SKILL.md", "skills/x/references/y.md",
     "docs/spec/requirements.md"],
)
def test_every_default_high_pattern_makes_a_candidate_high(path) -> None:
    risk = _classify([path])
    assert risk["tier"] == "high"
    assert risk["basis"] == "changed_paths"
    assert risk["reasons"][0].startswith(f"high:{path}")


@pytest.mark.parametrize(
    "path",
    ["src/hoh_control_plane/service/_common.py", "src/hoh_control_plane/domain.py",
     "src/hoh_control_plane/contracts.py", "src/hoh_control_plane/store.py",
     "src/hoh_control_plane/postgres_store.py"],
)
def test_every_default_medium_pattern_makes_a_candidate_medium(path) -> None:
    assert _classify([path])["tier"] == "medium"


@pytest.mark.parametrize(
    "path",
    ["docs/specs/requirements.md", "docs/README.md", "src/authz.py", "src/auth.py.bak",
     "src/hoh_control_plane/store_factory.py", "src/deploy.py", "myskills/x.md",
     "src/hoh_control_plane/service/qa.py"],
)
def test_near_misses_stay_low(path) -> None:
    assert _classify([path])["tier"] == "low"


def test_the_file_count_boundary_is_strictly_greater_than_the_limit() -> None:
    at_limit = [f"src/app/f{i}.py" for i in range(DEFAULT_MEDIUM_MAX_FILES)]
    assert _classify(at_limit)["tier"] == "low"
    over = at_limit + ["src/app/extra.py"]
    risk = _classify(over)
    assert risk["tier"] == "medium"
    assert risk["reasons"] == [f"medium:{len(over)} files > {DEFAULT_MEDIUM_MAX_FILES}"]
    assert _classify(["a.py", "b.py"], medium_max_files=1)["tier"] == "medium"
    assert _classify(["a.py"], medium_max_files=1)["tier"] == "low"


def test_high_wins_over_medium_and_count_and_reasons_are_capped() -> None:
    paths = [f"skills/s{i}/SKILL.md" for i in range(8)] + ["src/hoh_control_plane/domain.py"]
    risk = _classify(paths, medium_max_files=1)
    assert risk["tier"] == "high"
    assert len(risk["reasons"]) == 5
    assert all(reason.startswith("high:") for reason in risk["reasons"])


def test_one_reason_per_path_even_when_several_patterns_match() -> None:
    risk = _classify(["skills/auth.py"])
    assert risk["reasons"] == ["high:skills/auth.py ~ **/auth.py"]


def test_windows_separators_in_changed_paths_are_classified() -> None:
    assert _classify(["skills\\hoh-qa\\SKILL.md"])["tier"] == "high"


@pytest.mark.parametrize(
    ("scope", "tier"),
    [(["skills/**"], "high"), (["deploy/desktop/**"], "high"), (["docs/**"], "high"),
     (["docs/spec/**"], "high"), (["src/hoh_control_plane/store_schema.py"], "high"),
     (["src/hoh_control_plane/domain.py"], "medium"), (["src/app/**"], "low"),
     (["src/**"], "low"), ([], "low")],
)
def test_before_a_candidate_the_write_scope_is_the_basis(scope, tier) -> None:
    risk = _classify(None, scope)
    assert risk["tier"] == tier
    assert risk["basis"] == "write_scope"


def test_a_candidate_with_no_changed_paths_falls_back_to_the_scope() -> None:
    risk = _classify([], ["skills/**"])
    assert (risk["tier"], risk["basis"]) == ("high", "write_scope")


def test_empty_pattern_lists_leave_only_the_file_count() -> None:
    assert _classify(["skills/x.md"], high_risk_patterns=[], medium_risk_patterns=[])["tier"] == "low"


# ----------------------------------------------------------------- policy


def test_the_default_policy_is_reported_unconfigured(service, actors, seeded) -> None:
    policy = service.get_risk_policy(actors["qa"], "project-1")
    assert policy["configured"] is False
    assert policy["checker_distinct_from_maker"] is True
    assert policy["medium_max_files"] == DEFAULT_MEDIUM_MAX_FILES


@pytest.mark.parametrize("bad", [0, -1, 1001, "5", 2.5, None])
def test_medium_max_files_is_bounded(service, actors, seeded, bad) -> None:
    with pytest.raises(ValidationError):
        service.configure_risk_policy(actors["planner"], "project-1", medium_max_files=bad)


def test_a_boolean_is_not_a_file_count(service, actors, seeded) -> None:
    with pytest.raises(ValidationError):
        service.configure_risk_policy(actors["planner"], "project-1", medium_max_files=True)


@pytest.mark.parametrize("value", [1, 1000])
def test_medium_max_files_limits_are_inclusive(service, actors, seeded, value) -> None:
    policy = service.configure_risk_policy(actors["planner"], "project-1", medium_max_files=value)
    assert policy["medium_max_files"] == value
    assert policy["configured"] is True
    assert policy["configured_by"] == "planner-01"


def test_pattern_lists_are_bounded(service, actors, seeded) -> None:
    with pytest.raises(ValidationError):
        service.configure_risk_policy(actors["planner"], "project-1",
                                      high_risk_patterns=[f"p{i}/**" for i in range(51)])
    with pytest.raises(ValidationError):
        service.configure_risk_policy(actors["planner"], "project-1",
                                      medium_risk_patterns=["x" * 201])
    policy = service.configure_risk_policy(actors["planner"], "project-1",
                                           high_risk_patterns=[], medium_risk_patterns=None)
    assert policy["high_risk_patterns"] == []
    assert policy["medium_risk_patterns"] == list(DEFAULT_MEDIUM_RISK_PATTERNS)


@pytest.mark.parametrize("role", ["developer", "qa", "integrator"])
def test_only_planner_or_admin_configures_the_policy(service, actors, seeded, role) -> None:
    with pytest.raises(AuthorizationError):
        service.configure_risk_policy(actors[role], "project-1")


def test_reconfiguring_replaces_the_row_and_records_an_event(service, actors, seeded) -> None:
    service.configure_risk_policy(actors["planner"], "project-1", medium_max_files=4)
    admin = Actor("admin-01", Role.ADMIN, frozenset({"*"}))
    policy = service.configure_risk_policy(admin, "project-1", checker_distinct_from_maker=False)
    assert policy["medium_max_files"] == DEFAULT_MEDIUM_MAX_FILES
    assert policy["checker_distinct_from_maker"] is False
    assert policy["configured_by"] == "admin-01"
    assert len(events(service, "project.risk_policy_configured")) == 2


def test_a_policy_for_an_unknown_project_is_refused(service, actors, seeded) -> None:
    admin = Actor("admin-01", Role.ADMIN, frozenset({"*"}))
    with pytest.raises(Exception):
        service.configure_risk_policy(admin, "no-such-project")


# ---------------------------------------------------- packets and briefs


def test_every_role_packet_carries_the_tier(service, actors, seeded) -> None:
    service.configure_risk_policy(actors["planner"], "project-1",
                                  high_risk_patterns=["src/checkout/**"])
    for role in ("planner", "developer", "qa", "integrator"):
        packet = service.get_ticket_context(actors[role], "DEV-142")
        risk = packet["ticket"]["risk"]
        assert risk["tier"] == "high", role
        assert risk["basis"] == "write_scope"
        assert risk["requires"] == ["suite", "scenarios", "adversarial_review",
                                    "policy.approve_high_risk"]


def test_the_worker_brief_names_the_tier_and_what_it_requires(service, actors, seeded) -> None:
    service.configure_risk_policy(actors["planner"], "project-1",
                                  medium_risk_patterns=["src/checkout/**"])
    claim = service.claim_ticket(actors["developer"], "DEV-142",
                                 ticket(service, "DEV-142")["version"], 900)
    brief = claim["worker_brief"]
    assert brief["risk_tier"] == "medium"
    assert brief["risk_requires"] == ["suite", "scenarios", "adversarial_review"]


def test_a_low_tier_requires_only_suite_and_scenarios(service, actors, seeded) -> None:
    claim = service.claim_ticket(actors["developer"], "DEV-142",
                                 ticket(service, "DEV-142")["version"], 900)
    assert claim["worker_brief"]["risk_tier"] == "low"
    assert claim["worker_brief"]["risk_requires"] == ["suite", "scenarios"]


# ------------------------------------------------------------------ gate


REVIEW = finding("adversarial review", evidence_type="adversarial_review",
                 storage_ref="evidence://qa/review.json")


def _ready(service, actors, seeded, tmp_path, *, review: bool, **policy):
    service.configure_risk_policy(actors["planner"], "project-1", **policy)
    ready = verified_with_repository(
        service, actors, seeded, tmp_path, extra_findings=[REVIEW] if review else None
    )
    return ready, claim_integration(service, actors, ready["ticket_id"])


def test_a_low_tier_candidate_queues_without_review(service, actors, seeded, tmp_path) -> None:
    ready, integration = _ready(service, actors, seeded, tmp_path, review=False)
    assert queue_merge(service, actors, ready["ticket_id"], integration)["merge_job"]["status"] == "QUEUED"


def test_a_medium_candidate_without_review_is_refused(service, actors, seeded, tmp_path) -> None:
    ready, integration = _ready(service, actors, seeded, tmp_path, review=False,
                                medium_risk_patterns=["src/checkout/**"])
    with pytest.raises(ReleasePolicyError) as refused:
        queue_merge(service, actors, ready["ticket_id"], integration)
    assert refused.value.details["reason"] == "adversarial_review_missing"
    assert refused.value.details["risk"]["tier"] == "medium"
    with service.store.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM merge_jobs").fetchone()[0] == 0


def test_a_medium_candidate_with_review_queues(service, actors, seeded, tmp_path) -> None:
    ready, integration = _ready(service, actors, seeded, tmp_path, review=True,
                                medium_risk_patterns=["src/checkout/**"])
    assert queue_merge(service, actors, ready["ticket_id"], integration)["merge_job"]["status"] == "QUEUED"


def test_medium_by_file_count_needs_review(service, actors, seeded, tmp_path) -> None:
    service.configure_risk_policy(actors["planner"], "project-1", medium_max_files=1)
    ready = verified_with_repository(
        service, actors, seeded, tmp_path,
        files={"src/checkout/a.txt": "a\n", "src/checkout/b.txt": "b\n"},
    )
    integration = claim_integration(service, actors, ready["ticket_id"])
    with pytest.raises(ReleasePolicyError) as refused:
        queue_merge(service, actors, ready["ticket_id"], integration)
    assert refused.value.details["reason"] == "adversarial_review_missing"


def test_a_high_candidate_needs_review_first(service, actors, seeded, tmp_path) -> None:
    ready, integration = _ready(service, actors, seeded, tmp_path, review=False,
                                high_risk_patterns=["src/checkout/**"])
    service.approve_high_risk(actors["planner"], ready["ticket_id"], "Reviewed by the owner")
    with pytest.raises(ReleasePolicyError) as refused:
        queue_merge(service, actors, ready["ticket_id"], integration)
    assert refused.value.details["reason"] == "adversarial_review_missing"


def test_a_high_candidate_with_review_waits_for_the_sign_off(service, actors, seeded, tmp_path) -> None:
    ready, integration = _ready(service, actors, seeded, tmp_path, review=True,
                                high_risk_patterns=["src/checkout/**"])
    with pytest.raises(ReleasePolicyError) as refused:
        queue_merge(service, actors, ready["ticket_id"], integration)
    assert refused.value.details["reason"] == "high_risk_signoff_missing"
    assert refused.value.details["risk"]["high_risk_approved"] is False

    approval = service.approve_high_risk(actors["planner"], ready["ticket_id"], "Owner approved")

    assert approval["candidate_id"] == ready["candidate"]["candidate_id"]
    assert approval["risk"]["tier"] == "high"
    assert approval["risk"]["high_risk_approved"] is True
    assert queue_merge(service, actors, ready["ticket_id"], integration)["merge_job"]["status"] == "QUEUED"
    [event] = events(service, "policy.high_risk_approved", ready["ticket_id"])
    assert event["payload"]["candidate_id"] == ready["candidate"]["candidate_id"]


def test_a_sign_off_for_an_older_candidate_does_not_count(service, actors, seeded) -> None:
    service.configure_risk_policy(actors["planner"], "project-1",
                                  high_risk_patterns=["src/checkout/**"])
    first = submit_fake(service, actors, "DEV-142", attempt=1)
    service.approve_high_risk(actors["planner"], "DEV-142", "Approved the first attempt")
    criterion = ticket(service, "DEV-142")["acceptance_criteria"][0]
    from helpers.w7 import verdict
    verdict(service, actors, "DEV-142", "failed", [finding(criterion, "failed")])
    with service.store.connection(immediate=True) as connection:
        # The rework is a different frozen candidate with the same scope.
        connection.execute(
            "UPDATE tickets SET status = 'READY' WHERE ticket_id = 'DEV-142'"
        )
    second = submit_fake(service, actors, "DEV-142", attempt=2)
    assert second["candidate"]["candidate_id"] != first["candidate"]["candidate_id"]

    with service.store.connection() as connection:
        risk = service._ticket_risk(connection, "project-1", service._ticket(connection, "DEV-142"))
        with pytest.raises(ReleasePolicyError) as refused:
            connection.execute(
                "INSERT INTO evidence (evidence_id, candidate_id, criterion, evidence_type, "
                "observation, storage_ref, storage_checksum, checksum_source, created_by, created_at) "
                "VALUES ('ev-review', ?, 'review', 'adversarial_review', 'ok', 'evidence://qa/r', "
                "'x', 'claimed', 'qa-01', '2026-01-01T00:00:00Z')",
                (second["candidate"]["candidate_id"],),
            )
            service._require_risk_tier_cleared(
                connection, "project-1", service._ticket(connection, "DEV-142"),
                {"candidate_id": second["candidate"]["candidate_id"]},
            )
    assert risk["high_risk_approved"] is False
    assert refused.value.details["reason"] == "high_risk_signoff_missing"


def test_review_evidence_on_another_candidate_does_not_count(service, actors, seeded) -> None:
    service.configure_risk_policy(actors["planner"], "project-1",
                                  medium_risk_patterns=["src/checkout/**"])
    from helpers.w7 import open_ticket

    open_ticket(service, actors, "DEV-B", ["src/reporting/**"])
    other = submit_fake(service, actors, "DEV-B", developer="developer_2")
    submit_fake(service, actors, "DEV-142")
    with service.store.connection() as connection:
        connection.execute(
            "INSERT INTO evidence (evidence_id, candidate_id, criterion, evidence_type, "
            "observation, storage_ref, storage_checksum, checksum_source, created_by, created_at) "
            "VALUES ('ev-other', ?, 'review', 'adversarial_review', 'ok', "
            "'evidence://qa/r', 'x', 'claimed', 'qa-01', '2026-01-01T00:00:00Z')",
            (other["candidate"]["candidate_id"],),
        )
        current = connection.execute(
            "SELECT candidate_id FROM candidates WHERE ticket_id = 'DEV-142'"
        ).fetchone()["candidate_id"]
        with pytest.raises(ReleasePolicyError) as refused:
            service._require_risk_tier_cleared(
                connection, "project-1", service._ticket(connection, "DEV-142"),
                {"candidate_id": current},
            )
    assert refused.value.details["reason"] == "adversarial_review_missing"


# ------------------------------------------------------ approve_high_risk


@pytest.mark.parametrize("role", ["developer", "qa", "integrator"])
def test_only_planner_or_admin_signs_off(service, actors, seeded, role) -> None:
    submit_fake(service, actors, "DEV-142")
    with pytest.raises(AuthorizationError):
        service.approve_high_risk(actors[role], "DEV-142", "I approve")


def test_admin_may_sign_off(service, actors, seeded) -> None:
    submit_fake(service, actors, "DEV-142")
    admin = Actor("admin-01", Role.ADMIN, frozenset({"*"}))
    approval = service.approve_high_risk(admin, "DEV-142", "Operator approval")
    assert approval["approved_by"] == "admin-01"
    with service.store.connection() as connection:
        row = connection.execute("SELECT approved_role FROM high_risk_approvals").fetchone()
    assert row["approved_role"] == "admin"


@pytest.mark.parametrize("reason", ["", "   "])
def test_a_sign_off_needs_a_reason(service, actors, seeded, reason) -> None:
    submit_fake(service, actors, "DEV-142")
    with pytest.raises(ValidationError):
        service.approve_high_risk(actors["planner"], "DEV-142", reason)


def test_a_sign_off_reason_is_bounded(service, actors, seeded) -> None:
    submit_fake(service, actors, "DEV-142")
    with pytest.raises(ValidationError):
        service.approve_high_risk(actors["planner"], "DEV-142", "r" * 601)
    assert service.approve_high_risk(actors["planner"], "DEV-142", "r" * 600)["approval_id"]


def test_a_ticket_without_a_candidate_cannot_be_signed_off(service, actors, seeded) -> None:
    with pytest.raises(ConflictError):
        service.approve_high_risk(actors["planner"], "DEV-142", "Too early")


def test_a_planner_of_another_project_cannot_sign_off(service, actors, seeded) -> None:
    submit_fake(service, actors, "DEV-142")
    stranger = Actor("planner-x", Role.PLANNER, frozenset({"project-2"}))
    with pytest.raises(AuthorizationError):
        service.approve_high_risk(stranger, "DEV-142", "Not mine")


def test_the_sign_off_is_stored_trimmed_and_tied_to_the_candidate(service, actors, seeded) -> None:
    submitted = submit_fake(service, actors, "DEV-142")
    approval = service.approve_high_risk(actors["planner"], "DEV-142", "  approved  ")
    with service.store.connection() as connection:
        row = connection.execute(
            "SELECT * FROM high_risk_approvals WHERE approval_id = ?", (approval["approval_id"],)
        ).fetchone()
    assert row["reason"] == "approved"
    assert row["candidate_id"] == submitted["candidate"]["candidate_id"]


# ------------------------------------------------------------------- tools


@pytest.mark.anyio
async def test_the_risk_tools_are_planner_tools(service, actors, seeded) -> None:
    from hoh_control_plane.mcp_server import create_mcp_server

    planner_tools = {t.name for t in await create_mcp_server(service, actors["planner"]).list_tools()}
    qa_tools = {t.name for t in await create_mcp_server(service, actors["qa"]).list_tools()}
    assert {"planner.configure_risk_policy", "policy.approve_high_risk"} <= planner_tools
    assert not {"planner.configure_risk_policy", "policy.approve_high_risk"} & qa_tools


def test_discovery_names_the_risk_capabilities_for_planner(service, actors, seeded) -> None:
    from hoh_control_plane.service.discovery import CAPABILITY_TOOLS

    assert CAPABILITY_TOOLS["risk_policy:configure"] == "planner.configure_risk_policy"
    assert CAPABILITY_TOOLS["risk:approve_high"] == "policy.approve_high_risk"
