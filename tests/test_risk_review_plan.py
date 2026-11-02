"""The candidate, not the current project configuration, owns its review budget."""
import pytest

from helpers.w7 import finding, submit_fake, ticket
from hoh_control_plane.domain import Actor, AuthorizationError, ConflictError, ReleasePolicyError, Role
from hoh_control_plane.observability import WorkflowMetrics

pytestmark = pytest.mark.git


@pytest.fixture
def review_candidate(service, actors, seeded):
    def create(tier):
        service.configure_risk_policy(actors["planner"], "project-1",
                                      high_risk_patterns=["high/**"], medium_risk_patterns=["medium/**"])
        t = service.create_ticket(actors["planner"], "payment-retry", "RISK", "Risk review", "Verify plan",
                                  Role.DEVELOPER, ["Works"], [], [f"{tier}/**"], ready=True)
        candidate = submit_fake(service, actors, t["ticket_id"])["candidate"]
        # Repository-free fixtures supply a changed-path record for scoped briefs.
        with service.store.connection(immediate=True) as db:
            db.execute("INSERT INTO candidate_changed_paths(candidate_id,path) VALUES (?,?)",
                       (candidate["candidate_id"], f"{tier}/check.py"))
        return candidate
    return create


def _claim(service, actor):
    packet = service.get_ticket_context(actor, "RISK")
    return service.claim_qa(actor, "RISK", packet["ticket"]["version"], 900,
                            context_snapshot_id=packet["context_snapshot_id"])


def _verdict(service, actor, claim, candidate, outcome="passed"):
    packet = service.get_ticket_context(actor, "RISK")
    return service.submit_qa_verdict(actor, "RISK", claim["lease"]["lease_id"], candidate["candidate_id"],
        packet["ticket"]["version"], outcome, [finding("Works", "verified" if outcome == "passed" else "failed")],
        context_snapshot_id=packet["context_snapshot_id"])


@pytest.mark.parametrize("tier,owners,leaves", [("low", 1, 0), ("medium", 1, 0), ("high", 2, 3)])
def test_new_candidate_freezes_the_risk_directed_plan(service, actors, review_candidate, tier, owners, leaves):
    candidate = review_candidate(tier)
    plan = candidate["qa_review_plan"]
    assert plan["tier"] == tier
    assert plan["required_qa_reviews"] == candidate["required_qa_reviews"] == owners
    assert plan["advisory_reviewers_per_owner"] == leaves
    assert plan["suite"] == ("focused" if tier == "low" else "shared_full_suite")
    service.configure_risk_policy(actors["planner"], "project-1", high_risk_patterns=["**"], medium_risk_patterns=[])
    packet = service.get_ticket_context(actors["qa"], "RISK")
    assert packet["ticket"]["risk"]["tier"] == tier
    assert packet["candidate"]["qa_review_plan"] == plan
    claim = _claim(service, actors["qa"])
    brief = claim["worker_brief"]
    assert brief["review_plan"] == plan
    assert bool(brief["review_delegation"]) == bool(leaves)
    if not leaves:
        result = _verdict(service, actors["qa"], claim, candidate)
        assert result["aggregate_verdict"] == "passed" and result["ticket"]["status"] == "VERIFIED"
        peer = Actor("qa-peer", Role.QA, frozenset({"project-1"}))
        with pytest.raises(ConflictError):
            _claim(service, peer)


def _complete_batch(service, actor, claim, candidate):
    lease = claim["lease"]["lease_id"]
    scopes = [dict(task_key=key, paths=["high/check.py"], related_paths=[], question=f"Review {key}") for key in "ABC"]
    batch = service.prepare_review_batch(actor, "RISK", lease, scopes)
    for key in "ABC":
        batch = service.checkpoint_review_task(actor, "RISK", lease, batch["batch_id"], batch["revision"],
            key, f"native-{actor.agent_id}-{key}", "completed", f"evidence://{candidate['candidate_id']}/{actor.agent_id}/{key}")
    assert batch["ready_for_synthesis"] is True


def test_high_needs_distinct_owners_and_completed_advisories(service, actors, review_candidate):
    candidate = review_candidate("high")
    first = _claim(service, actors["qa"])
    peer = Actor("qa-peer", Role.QA, frozenset({"project-1"}))
    second = _claim(service, peer)
    with pytest.raises(ConflictError):
        _claim(service, actors["qa"])
    with pytest.raises(ConflictError, match="review"):
        _verdict(service, actors["qa"], first, candidate)
    _complete_batch(service, actors["qa"], first, candidate)
    from hoh_control_plane.domain import ContextStaleError
    with pytest.raises(ContextStaleError):
        service.submit_qa_verdict(actors["qa"], "RISK", first["lease"]["lease_id"], candidate["candidate_id"],
            ticket(service, "RISK")["version"], "passed", [finding("Works")],
            context_snapshot_id=first["current_context_snapshot_id"])
    partial = _verdict(service, actors["qa"], first, candidate)
    assert partial["aggregate_verdict"] == "pending"
    assert partial["ticket"]["status"] == "QA_IN_PROGRESS"
    with service.store.connection() as db:
        with pytest.raises(ReleasePolicyError):
            service._require_qa_reviews_passed(db, candidate)
    _complete_batch(service, peer, second, candidate)
    passed = _verdict(service, peer, second, candidate)
    assert passed["aggregate_verdict"] == "passed" and passed["ticket"]["status"] == "VERIFIED"
    assert len({r["reviewer_id"] for r in passed["qa_reviews"] if r["status"] == "PASSED"}) == 2
    with service.store.connection() as db:
        service._require_qa_reviews_passed(db, candidate)


def test_maker_identity_is_always_refused(service, actors, review_candidate):
    review_candidate("low")
    maker = Actor(actors["developer"].agent_id, Role.QA, frozenset({"project-1"}))
    with pytest.raises(AuthorizationError):
        _claim(service, maker)


def test_historical_recorded_requirement_is_not_reclassified(service, actors, review_candidate):
    candidate = review_candidate("high")
    with service.store.connection(immediate=True) as db:
        db.execute("UPDATE candidates SET qa_review_plan=NULL, required_qa_reviews=1 WHERE candidate_id=?",
                   (candidate["candidate_id"],))
    claim = _claim(service, actors["qa"])
    result = _verdict(service, actors["qa"], claim, candidate)
    assert result["aggregate_verdict"] == "passed"
    with service.store.connection() as db:
        row = db.execute("SELECT required_qa_reviews, qa_review_plan FROM candidates WHERE candidate_id=?",
                         (candidate["candidate_id"],)).fetchone()
        assert tuple(row) == (1, None)


def test_failed_owner_and_expired_peer_settle_once(service, actors, review_candidate):
    candidate = review_candidate("high")
    first = _claim(service, actors["qa"])
    peer = Actor("qa-peer", Role.QA, frozenset({"project-1"}))
    second = _claim(service, peer)
    _complete_batch(service, actors["qa"], first, candidate)
    assert _verdict(service, actors["qa"], first, candidate, "failed")["aggregate_verdict"] == "pending"
    assert WorkflowMetrics(service.store).project_metrics(actors["planner"], "project-1")["rework"]["qa_failures"] == 0
    with service.store.connection(immediate=True) as db:
        db.execute("UPDATE leases SET expires_at='2000-01-01T00:00:00Z' WHERE lease_id=?", (second["lease"]["lease_id"],))
    admin = Actor("sweeper", Role.ADMIN, frozenset({"*"}))
    service.sweep_expired_leases(admin)
    assert ticket(service, "RISK")["status"] == "REWORK"
    service.sweep_expired_leases(admin)
    metrics = WorkflowMetrics(service.store).project_metrics(actors["planner"], "project-1")["rework"]
    assert metrics["qa_failures"] == 1 and metrics["candidate_pass_rate"] == 0.0


def test_advisory_checkpoint_is_private_idempotent_and_bounded(service, actors, review_candidate):
    from hoh_control_plane.domain import ValidationError
    candidate = review_candidate("high")
    first = _claim(service, actors["qa"])
    peer = Actor("qa-peer", Role.QA, frozenset({"project-1"}))
    second = _claim(service, peer)
    scopes = [dict(task_key=key, paths=["high/check.py"], question=f"Review {key}") for key in "ABC"]
    with pytest.raises(ValidationError):
        service.prepare_review_batch(actors["qa"], "RISK", first["lease"]["lease_id"], scopes[:2])
    batch = service.prepare_review_batch(actors["qa"], "RISK", first["lease"]["lease_id"], scopes)
    assert service.get_review_batch(peer, "RISK", second["lease"]["lease_id"]) is None
    with pytest.raises(AuthorizationError):
        service.resolve_context_reference(peer, "ctx:entry:" + batch["batch_id"])
    packet = service.get_ticket_context(peer, "RISK")
    assert all(e["entry_type"] != "qa_review_batch" for e in packet["context_entries"])
    args = (actors["qa"], "RISK", first["lease"]["lease_id"], batch["batch_id"], 0, "A", "task-A", "completed", "evidence://A")
    changed = service.checkpoint_review_task(*args)
    assert service.checkpoint_review_task(*args) == changed
    with pytest.raises(ConflictError, match="stale"):
        service.checkpoint_review_task(actors["qa"], "RISK", first["lease"]["lease_id"], batch["batch_id"],
                                      0, "B", "task-B", "completed", "evidence://B")
    with pytest.raises(ConflictError, match="distinct"):
        service.checkpoint_review_task(actors["qa"], "RISK", first["lease"]["lease_id"], batch["batch_id"],
                                      1, "B", "task-A", "completed", "evidence://B")


def test_shared_suite_is_candidate_owned_and_new_attempt_fences_old_green(service, actors, seeded, tmp_path):
    import sys
    from helpers.qa import candidate_waiting_for_qa_run
    from helpers.w7 import complete_advisory_review
    service.configure_risk_policy(actors["planner"], "project-1", high_risk_patterns=["src/checkout/**"])
    t, submitted, first = candidate_waiting_for_qa_run(service, actors, seeded, tmp_path,
                                                     [sys.executable, "-c", "print('passed')"])
    ticket_id, candidate = t["ticket_id"], submitted["candidate"]
    peer = Actor("qa-peer", Role.QA, frozenset({"project-1"}))
    second = service.claim_qa(peer, ticket_id, ticket(service, ticket_id)["version"], 900)
    queued = service.queue_project_tests(actors["qa"], ticket_id, first["lease"]["lease_id"])
    shared = service.queue_project_tests(peer, ticket_id, second["lease"]["lease_id"])
    assert shared["reused"] is True and shared["test_run"]["test_run_id"] == queued["test_run"]["test_run_id"]
    complete_advisory_review(service, actors["qa"], ticket_id, first["lease"]["lease_id"], candidate)
    packet = service.get_ticket_context(actors["qa"], ticket_id)
    partial = service.submit_qa_verdict(actors["qa"], ticket_id, first["lease"]["lease_id"], candidate["candidate_id"],
        packet["ticket"]["version"], "passed", [finding(c) for c in t["acceptance_criteria"]],
        context_snapshot_id=packet["context_snapshot_id"])
    assert partial["aggregate_verdict"] == "pending"
    run_id = queued["test_run"]["test_run_id"]
    complete_advisory_review(service, peer, ticket_id, second["lease"]["lease_id"], candidate)
    packet = service.get_ticket_context(peer, ticket_id)
    with pytest.raises(ReleasePolicyError) as pending_verdict:
        service.submit_qa_verdict(peer, ticket_id, second["lease"]["lease_id"], candidate["candidate_id"],
            packet["ticket"]["version"], "passed", [finding(c) for c in t["acceptance_criteria"]],
            context_snapshot_id=packet["context_snapshot_id"])
    assert pending_verdict.value.details["reason"] == "regression_run_pending"
    completed = service.execute_queued_project_test(run_id)
    assert completed["status"] == "COMPLETED" and completed["termination_reason"] == "passed"
    observed = service.get_project_test_run(peer, ticket_id, second["lease"]["lease_id"], run_id)
    assert observed["lease"]["agent_id"] == peer.agent_id
    assert service.get_project_test_run(actors["qa"], ticket_id, first["lease"]["lease_id"], run_id)["lease"] is None
    with service.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM qa_test_jobs").fetchone()[0] == 1
        profile = service._one(db, "SELECT * FROM candidate_test_profiles WHERE candidate_id=?", (candidate["candidate_id"],))
    assert service._reusable_qa_run(candidate["candidate_id"], profile["test_command"], profile["timeout_seconds"]) == run_id
    assert service._reusable_qa_run(candidate["candidate_id"], [sys.executable, "-c", "print('other')"], 120) is None
    assert service._reusable_qa_run(candidate["candidate_id"], profile["test_command"], profile["timeout_seconds"] + 1) is None
    retry = service.queue_project_tests(peer, ticket_id, second["lease"]["lease_id"], rerun=True)
    assert retry["test_run"]["test_run_id"] != run_id
    assert service._reusable_qa_run(candidate["candidate_id"], profile["test_command"], profile["timeout_seconds"]) is None
    with service.store.connection() as db:
        state = service._candidate_suite_state(db, candidate["candidate_id"], profile["test_command"], profile["timeout_seconds"])
    with pytest.raises(ReleasePolicyError) as pending:
        service._require_regression_run(state)
    assert pending.value.details["reason"] == "regression_run_pending"


@pytest.mark.parametrize("old_schema,recorded_count", [(3, 1), (4, 2)])
def test_migration_preserves_recorded_counts_and_live_legacy_review(service, store, actors, seeded,
                                                                  old_schema, recorded_count):
    submitted = submit_fake(service, actors, seeded["ticket"]["ticket_id"])
    candidate_id = submitted["candidate"]["candidate_id"]
    claim = service.claim_qa(actors["qa"], seeded["ticket"]["ticket_id"], submitted["ticket"]["version"], 900)
    with store.connection(immediate=True) as db:
        original = dict(db.execute("SELECT * FROM candidates WHERE candidate_id=?", (candidate_id,)).fetchone())
        db.execute("ALTER TABLE candidates DROP COLUMN qa_review_plan")
        if old_schema == 3:
            db.execute("DELETE FROM qa_reviews")
            db.execute("ALTER TABLE candidates DROP COLUMN required_qa_reviews")
            db.execute("CREATE UNIQUE INDEX leases_one_per_ticket ON leases(ticket_id)")
        else:
            db.execute("UPDATE candidates SET required_qa_reviews=2 WHERE candidate_id=?", (candidate_id,))
        db.execute("UPDATE schema_meta SET value=? WHERE key='schema_version'", (str(old_schema),))
    store.initialize()
    store.initialize()  # Idempotent upgrade, never duplicate live legacy attempts.
    with store.connection() as db:
        migrated = dict(db.execute("SELECT * FROM candidates WHERE candidate_id=?", (candidate_id,)).fetchone())
        assert migrated["qa_review_plan"] is None
        assert migrated["required_qa_reviews"] == recorded_count
        assert {k: v for k, v in migrated.items() if k not in {"qa_review_plan", "required_qa_reviews"}} == {
            k: v for k, v in original.items() if k not in {"qa_review_plan", "required_qa_reviews"}}
        reviews = service._candidate_qa_reviews(db, candidate_id)
        assert len(reviews) == 1 and reviews[0]["lease_id"] == claim["lease"]["lease_id"]
        assert reviews[0]["status"] == "IN_PROGRESS"
