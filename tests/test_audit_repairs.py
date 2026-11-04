"""Accepted a58 audit oracles, using disposable state and existing repo helpers."""
import json
import sys
from types import SimpleNamespace

import pytest

from helpers.w7 import finding, pass_all, submit_fake, ticket
from hoh_control_plane.domain import Actor, ConflictError, ReleasePolicyError, Role, ValidationError
from hoh_control_plane.supervisor import Supervisor


@pytest.fixture
def artifact(service, actors, seeded):
    def create(tier="high", path="high/check.py"):
        service.configure_risk_policy(actors["planner"], "project-1", high_risk_patterns=["high/**"], medium_risk_patterns=["medium/**"])
        service.create_ticket(actors["planner"], "payment-retry", "CASE", "Case", "Observe boundaries",
                              Role.DEVELOPER, ["Works"], [], [f"{tier}/**"], ready=True)
        candidate = submit_fake(service, actors, "CASE")["candidate"]
        with service.store.connection(immediate=True) as db:
            db.execute("INSERT INTO candidate_changed_paths VALUES (?,?)", (candidate["candidate_id"], path))
        return candidate
    return create


def claim(service, actor):
    return service.claim_qa(actor, "CASE", ticket(service, "CASE")["version"], 900)


def verdict(service, actor, owner, candidate, status="verified", evidence_type="test_result"):
    packet = service.get_ticket_context(actor, "CASE")
    return service.submit_qa_verdict(actor, "CASE", owner["lease"]["lease_id"], candidate["candidate_id"],
        packet["ticket"]["version"], "passed" if status == "verified" else "failed",
        [finding("Works", status, evidence_type=evidence_type)], context_snapshot_id=packet["context_snapshot_id"])


@pytest.mark.parametrize("path", ["high/check.py", "high/[tenant]/auth.py"])
def test_exact_literal_advisory_paths(service, actors, artifact, path):
    artifact(path=path)
    owner = claim(service, actors["qa"])
    batch = service.prepare_review_batch(actors["qa"], "CASE", owner["lease"]["lease_id"],
        [dict(task_key=k, paths=[path], question="Review this literal file") for k in "ABC"])
    assert [t["task_key"] for t in batch["tasks"]] == list("ABC")
    assert all(t["brief"]["paths"] == [path] and t["brief"]["diff_argv"][-1] == ":(literal)" + path for t in batch["tasks"])


@pytest.mark.parametrize("paths", [["../high/check.py"], ["high/*.py"], ["high/"], ["other.py"],
                                  ["high/check.py"] * 2, [f"high/{i}.py" for i in range(9)]])
def test_advisory_path_boundaries(service, actors, artifact, paths):
    artifact()
    owner = claim(service, actors["qa"])
    with pytest.raises(ValidationError):
        service.prepare_review_batch(actors["qa"], "CASE", owner["lease"]["lease_id"],
            [dict(task_key=k, paths=paths, question="Review scope") for k in "ABC"])


@pytest.mark.parametrize("foreign_status", ["BACKLOG", "DONE"])
def test_preserved_foreign_edge_is_private_and_side_effect_free(service, actors, seeded, foreign_status):
    foreign = Actor("foreign-planner", Role.PLANNER, frozenset({"foreign"}))
    service.create_project(foreign, "foreign", "Private", "Private", "main")
    service.create_feature(foreign, "foreign", "foreign-feature", "Private", "Private")
    service.create_ticket(foreign, "foreign-feature", "PRIVATE-ID", "Private title", "Private objective",
                          Role.DEVELOPER, ["Private criterion"], [], ["private/**"])
    local = seeded["ticket"]["ticket_id"]
    with service.store.connection(immediate=True) as db:
        db.execute("UPDATE tickets SET status=? WHERE ticket_id='PRIVATE-ID'", (foreign_status,))
        db.execute("INSERT INTO ticket_dependencies VALUES (?,?)", (local, "PRIVATE-ID"))
        before = [tuple(r) for r in db.execute("SELECT * FROM dispatch_outbox").fetchall()]
    packet = service.get_ticket_context(actors["developer"], local)
    with pytest.raises(ConflictError) as denied:
        service.claim_ticket(actors["developer"], local, packet["ticket"]["version"], 900,
                              context_snapshot_id=packet["context_snapshot_id"])
    assert denied.value.details == {"reason": "dependency_project_mismatch"}
    for secret in ("PRIVATE-ID", "Private title", "Private objective", "Private criterion"):
        assert secret not in str(packet) + str(denied.value) + str(denied.value.details)
    with service.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM leases WHERE ticket_id=?", (local,)).fetchone()[0] == 0
        assert service._ticket(db, local)["version"] == packet["ticket"]["version"]
        assert [tuple(r) for r in db.execute("SELECT * FROM dispatch_outbox").fetchall()] == before
        assert db.execute("SELECT depends_on_ticket_id FROM ticket_dependencies WHERE ticket_id=?", (local,)).fetchone()[0] == "PRIVATE-ID"


def test_finished_local_dependency_allows_claim(service, actors, seeded):
    service.create_ticket(actors["planner"], "payment-retry", "DEP", "Dependency", "Done",
                          Role.DEVELOPER, ["Works"], [], ["dep/**"])
    with service.store.connection(immediate=True) as db:
        db.execute("UPDATE tickets SET status='DONE' WHERE ticket_id='DEP'")
    t = service.create_ticket(actors["planner"], "payment-retry", "LOCAL", "Local", "Local",
        Role.DEVELOPER, ["Works"], [], ["local/**"], ready=True, dependency_ids=["DEP"])
    assert service.claim_ticket(actors["developer"], "LOCAL", t["version"], 900)["ticket"]["status"] == "DEV_IN_PROGRESS"


@pytest.mark.parametrize("disjoint", [False, True])
def test_derived_publication_freezes_fresh_authority(service, actors, artifact, disjoint):
    original = artifact()
    with service.store.connection(immediate=True) as db:
        db.execute("UPDATE candidates SET qa_review_plan=NULL,required_qa_reviews=1 WHERE candidate_id=?", (original["candidate_id"],))
    verified = pass_all(service, actors, "CASE", extra=[finding("Adversarial review", evidence_type="adversarial_review")])
    service.approve_high_risk(actors["planner"], "CASE", "Historical approval")
    owner = service.claim_integration(actors["integrator"], "CASE", verified["ticket"]["version"], 900)
    with service.store.connection(immediate=True) as db:
        db.execute("INSERT INTO merge_jobs(merge_job_id,project_id,ticket_id,lease_id,expected_version,candidate_id,requested_by,status,created_at) "
                   "VALUES ('fixture-merge','project-1','CASE',?,?,?,?,'RUNNING','2026-01-01T00:00:00Z')",
                   (owner["lease"]["lease_id"], owner["ticket"]["version"], original["candidate_id"], actors["integrator"].agent_id))
        job = service._merge_job(db, "fixture-merge")
        historical = service._one(db, "SELECT * FROM candidates WHERE candidate_id=?", (original["candidate_id"],))
    paths = ["other/check.py"] if disjoint else ["high/peer.py"]
    assert (not any(service._scope_roots_overlap(p, q) for p in paths for q in ["high/check.py", "high/**"])) is disjoint
    result = service._record_rebased_candidate("fixture-merge", job, actors["integrator"], historical,
        "c" * 40, "d" * 40, ["high/check.py"], paths, disjoint, 0, "fixture-r1")
    assert result["status"] == "COMPLETED" and ticket(service, "CASE")["status"] == "QA_READY"
    with service.store.connection() as db:
        derived = service._one(db, "SELECT * FROM candidates WHERE derived_from=?", (original["candidate_id"],))
        assert service._one(db, "SELECT * FROM candidates WHERE candidate_id=?", (original["candidate_id"],)) == historical
        assert derived["qa_evidence_from"] is None and derived["commit_sha"] == "d" * 40
        assert derived["qa_review_plan"]["required_qa_reviews"] == derived["required_qa_reviews"] == 2
        assert derived["qa_review_plan"]["advisory_reviewers_per_owner"] == 3
        assert service._high_risk_approval(db, derived["candidate_id"]) is None
        with pytest.raises(ReleasePolicyError):
            service._require_qa_reviews_passed(db, derived)
    service.configure_risk_policy(actors["planner"], "project-1", high_risk_patterns=[], medium_risk_patterns=[])
    assert service.get_ticket_context(actors["qa"], "CASE")["candidate"]["qa_review_plan"] == derived["qa_review_plan"]
    fresh = claim(service, actors["qa"])
    with pytest.raises(ConflictError, match="frozen ticket artifact"):
        verdict(service, actors["qa"], fresh, original)


def test_replaced_adversarial_evidence_is_not_authority(service, actors, artifact):
    candidate = artifact("medium", "medium/check.py")
    first = claim(service, actors["qa"])
    failed = verdict(service, actors["qa"], first, candidate, "insufficient_evidence", "adversarial_review")
    finding_id = failed["evidence_bundle"]["gap_records"][0]["finding_id"]
    readiness = service.append_planner_context(actors["planner"], ticket_id="CASE", entry_type="planner_decision",
        content="Infrastructure ready", source_ref="evidence://fixture/readiness")
    service.recover_candidate_for_qa(actors["planner"], "CASE", ticket(service, "CASE")["version"], finding_id, readiness["entry_id"])
    current = verdict(service, actors["qa"], claim(service, actors["qa"]), candidate)
    with service.store.connection() as db:
        assert db.execute("SELECT status FROM qa_reviews WHERE review_id=?", (first["review"]["review_id"],)).fetchone()[0] == "REPLACED"
        with pytest.raises(ReleasePolicyError) as denied:
            service._require_risk_tier_cleared(db, "project-1", ticket(service, "CASE"), current["candidate"])
    assert denied.value.details["reason"] == "adversarial_review_missing"


@pytest.mark.parametrize("invalid", [None, "FAILED", "EXPIRED", "REPLACED", "unreferenced", "failed-finding"])
def test_adversarial_requires_current_verified_reference(service, actors, artifact, invalid):
    candidate = artifact("medium", "medium/check.py")
    owner = claim(service, actors["qa"])
    result = verdict(service, actors["qa"], owner, candidate, evidence_type="adversarial_review")
    with service.store.connection(immediate=True) as db:
        if invalid in {"FAILED", "EXPIRED", "REPLACED"}:
            db.execute("UPDATE qa_reviews SET status=? WHERE review_id=?", (invalid, owner["review"]["review_id"]))
        elif invalid == "unreferenced":
            db.execute("UPDATE qa_findings SET evidence_refs='[]' WHERE candidate_id=?", (candidate["candidate_id"],))
        elif invalid == "failed-finding":
            db.execute("UPDATE qa_findings SET status='failed' WHERE candidate_id=?", (candidate["candidate_id"],))
        if invalid is None:
            service._require_risk_tier_cleared(db, "project-1", result["ticket"], candidate)
        else:
            with pytest.raises(ReleasePolicyError) as denied:
                service._require_risk_tier_cleared(db, "project-1", result["ticket"], candidate)
            assert denied.value.details["reason"] == "adversarial_review_missing"


def test_untouched_historical_adversarial_reference_remains_eligible(service, actors, artifact):
    candidate = artifact("medium", "medium/check.py")
    owner = claim(service, actors["qa"])
    result = verdict(service, actors["qa"], owner, candidate, evidence_type="adversarial_review")
    with service.store.connection(immediate=True) as db:
        db.execute("UPDATE candidates SET qa_review_plan=NULL WHERE candidate_id=?", (candidate["candidate_id"],))
        db.execute("UPDATE qa_findings SET review_id=NULL WHERE candidate_id=?", (candidate["candidate_id"],))
        db.execute("UPDATE evidence SET review_id=NULL WHERE candidate_id=?", (candidate["candidate_id"],))
        db.execute("DELETE FROM qa_reviews WHERE candidate_id=?", (candidate["candidate_id"],))
        service._require_risk_tier_cleared(db, "project-1", result["ticket"], candidate)


@pytest.mark.parametrize("requester_end", ["completed", "expired"])
def test_real_watchdog_sustains_peer_and_revokes_invalid_authority(service, actors, artifact, monkeypatch, requester_end):
    candidate = artifact()
    first = claim(service, actors["qa"])
    peer = Actor("qa-peer", Role.QA, frozenset({"project-1"}))
    second = claim(service, peer)
    # Only the external managed-process boundary is replaced. Queue provenance
    # models an admitted worker, while cancellation reads real lifecycle state.
    command = [sys.executable, "-c", "print('ok')"]
    with service.store.connection(immediate=True) as db:
        db.execute("INSERT INTO qa_test_jobs(test_run_id,project_id,candidate_id,ticket_id,lease_id,test_command,command_digest,timeout_seconds,worktree_path,candidate_commit,requested_by,status,created_at) "
                   "VALUES ('testrun_watch','project-1',?,'CASE',?,?,'fixture',120,'fixture',?,?,'QUEUED','2026-01-01T00:00:00Z')",
                   (candidate["candidate_id"], first["lease"]["lease_id"], json.dumps(command), candidate["commit_sha"], actors["qa"].agent_id))
    supervisor = Supervisor(service, worker_timeout=120)
    import itertools
    clock = itertools.count(0, 2)
    monkeypatch.setattr("hoh_control_plane.supervisor.time.monotonic", lambda: next(clock))
    decisions = []
    def managed(*args, **kwargs):
        cancel = kwargs["cancel"]
        assert kwargs["timeout"] == 120
        decisions.append(cancel())
        if requester_end == "completed":
            from helpers.w7 import complete_advisory_review
            complete_advisory_review(service, actors["qa"], "CASE", first["lease"]["lease_id"], candidate)
            assert verdict(service, actors["qa"], first, candidate)["aggregate_verdict"] == "pending"
        else:
            with service.store.connection(immediate=True) as db:
                db.execute("UPDATE leases SET expires_at='2000-01-01T00:00:00Z' WHERE lease_id=?", (first["lease"]["lease_id"],))
            service.sweep_expired_leases(Actor("sweeper", Role.ADMIN, frozenset({"*"})))
        decisions.append(cancel())
        observed = service.get_project_test_run(peer, "CASE", second["lease"]["lease_id"], "testrun_watch")
        assert observed["lease"]["agent_id"] == peer.agent_id
        with service.store.connection() as db:
            assert db.execute("SELECT COUNT(*) FROM leases WHERE lease_id=?", (first["lease"]["lease_id"],)).fetchone()[0] == 0
            assert service._qa_test_job(db, "testrun_watch")["lease_id"] == first["lease"]["lease_id"]
        supervisor.stopping.set()
        assert cancel() is True
        supervisor.stopping.clear()
        with service.store.connection(immediate=True) as db:
            db.execute("UPDATE tickets SET candidate_commit=? WHERE ticket_id='CASE'", ("e" * 40,))
        assert cancel() is True
        with service.store.connection(immediate=True) as db:
            db.execute("UPDATE tickets SET candidate_commit=? WHERE ticket_id='CASE'", (candidate["commit_sha"],))
        assert cancel() is False
        with service.store.connection(immediate=True) as db:
            db.execute("UPDATE leases SET expires_at='2000-01-01T00:00:00Z' WHERE lease_id=?", (second["lease"]["lease_id"],))
        assert cancel() is True
        with service.store.connection(immediate=True) as db:
            db.execute("UPDATE leases SET expires_at='2099-01-01T00:00:00Z' WHERE lease_id=?", (second["lease"]["lease_id"],))
            reconciliation = service._open_commit_reconciliation(db, actors["planner"], "project-1",
                commit=candidate["commit_sha"], subject="Fixture retirement", relation="qa_failed",
                observation_ref="evidence://fixture/retirement", frozen_ticket_ids=["CASE"])
            # A durable pending retirement must revoke execution before finalization.
            db.execute("INSERT INTO ticket_retirement_requests(request_id,project_id,ticket_id,reconciliation_id,target_status,reason,requested_by,status,requested_at) "
                       "VALUES ('retire-fixture','project-1','CASE',?,'CANCELLED','Fixture retirement','planner','PENDING','2026-01-01T00:00:00Z')", (reconciliation,))
        assert cancel() is True
        return SimpleNamespace(cancelled=False, timed_out=False, returncode=0)
    monkeypatch.setattr("hoh_control_plane.supervisor.run_managed", managed)
    supervisor._execute("fixture-execution", "qa", "testrun_watch")
    assert decisions == [False, False]
