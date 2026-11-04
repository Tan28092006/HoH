"""Regressions from the retained operational source audit, with disposable state."""
import pytest

from helpers.w7 import commit_file, finding, repository, submit_fake, verdict
from hoh_control_plane.domain import Actor, Role, ValidationError
from hoh_control_plane.observability import WorkflowMetrics


def test_equivalent_landing_keeps_python_indentation(service, tmp_path):
    repo = repository(tmp_path)
    base = commit_file(repo, "allow.py", "def allow(ok):\n    if ok:\n        return True\n    return False\n")
    from helpers.w7 import git
    git(repo, "checkout", "-b", "candidate")
    candidate = commit_file(repo, "allow.py", "def allow(ok):\n    if ok:\n        return True\n    return True\n")
    git(repo, "checkout", "main")
    commit_file(repo, "allow.py", "def allow(ok):\n    if ok:\n        return True\n        return True\n")
    assert service.git_worktrees.equivalent_landing(
        repository_path=str(repo), default_branch="main", base_commit=base,
        candidate_commit=candidate,
    ) is None


def test_create_dependency_refuses_foreign_project_without_writing(service, actors, seeded):
    foreign = Actor("foreign-planner", Role.PLANNER, frozenset({"foreign"}))
    service.create_project(foreign, "foreign", "Foreign", "Private", "main")
    service.create_feature(foreign, "foreign", "foreign-feature", "Private", "Private")
    service.create_ticket(foreign, "foreign-feature", "FOREIGN", "Private title", "Private objective",
                          Role.DEVELOPER, ["Private criterion"], [], [])
    with pytest.raises(ValidationError, match="same project"):
        service.create_ticket(actors["planner"], "payment-retry", "LOCAL", "Local", "Local",
                              Role.DEVELOPER, ["Works"], [], [], dependency_ids=["FOREIGN"])
    with service.store.connection() as db:
        assert db.execute("SELECT 1 FROM tickets WHERE ticket_id='LOCAL'").fetchone() is None
        assert db.execute("SELECT 1 FROM ticket_dependencies WHERE ticket_id='LOCAL'").fetchone() is None


def test_metrics_count_settled_current_candidates(service, actors, seeded):
    ticket_id = seeded["ticket"]["ticket_id"]
    submit_fake(service, actors, ticket_id)
    criteria = seeded["ticket"]["acceptance_criteria"]
    verdict(service, actors, ticket_id, "passed", [finding(c) for c in criteria])
    service.create_ticket(actors["planner"], "payment-retry", "FAILED", "Failed", "Failed",
                          Role.DEVELOPER, ["Works"], [], ["other/**"], ready=True)
    submit_fake(service, actors, "FAILED", attempt=2)
    verdict(service, actors, "FAILED", "failed", [finding("Works", "failed")])
    actual = WorkflowMetrics(service.store).project_metrics(actors["planner"], "project-1")["rework"]
    assert actual == dict(qa_passes=1, qa_failures=1, candidate_pass_rate=0.5, base_drift=0, blocked=0)


def test_readiness_dedup_preserves_fresh_source(service, actors, seeded):
    args = dict(ticket_id=seeded["ticket"]["ticket_id"], entry_type="planner_decision",
                content="Host ready after remediation")
    first = service.append_planner_context(actors["planner"], **args, source_ref="evidence://before")
    repeat = service.append_planner_context(actors["planner"], **args, source_ref="evidence://before")
    assert repeat["entry_id"] == first["entry_id"] and repeat["deduplicated"] is True
    fresh = service.append_planner_context(actors["planner"], **args, source_ref="evidence://after")
    assert fresh["entry_id"] != first["entry_id"]
    assert fresh["source_ref"] == "evidence://after" and fresh["deduplicated"] is False
    with service.store.connection() as db:
        assert db.execute("SELECT source_ref FROM context_entries WHERE entry_id=?",
                          (first["entry_id"],)).fetchone()[0] == "evidence://before"


def test_existing_foreign_dependency_metadata_is_not_projected(service, actors, seeded):
    foreign = Actor("foreign-planner", Role.PLANNER, frozenset({"foreign"}))
    service.create_project(foreign, "foreign", "Foreign", "Private", "main")
    service.create_feature(foreign, "foreign", "foreign-feature", "Private", "Private")
    service.create_ticket(foreign, "foreign-feature", "FOREIGN", "Private title", "Private objective",
                          Role.DEVELOPER, ["Private criterion"], [], [])
    # A legacy edge is preserved in storage, but must not disclose its endpoint.
    with service.store.connection(immediate=True) as db:
        db.execute("INSERT INTO ticket_dependencies VALUES (?,?)", (seeded["ticket"]["ticket_id"], "FOREIGN"))
    packet = service.get_ticket_context(actors["planner"], seeded["ticket"]["ticket_id"])
    assert packet["dependencies"] == []
    assert packet["ticket"]["dependency_ids"] == []
    assert "Private title" not in str(packet) and "FOREIGN" not in str(packet)


def test_fresh_readiness_source_recovers_the_same_candidate(service, actors, seeded, tmp_path, monkeypatch):
    import sys
    from helpers.qa import candidate_waiting_for_qa_run
    t, submitted, claim = candidate_waiting_for_qa_run(service, actors, seeded, tmp_path,
                                                     [sys.executable, "-c", "print('passed')"])
    args = dict(ticket_id=t["ticket_id"], entry_type="planner_decision", content="Host ready after remediation")
    with monkeypatch.context() as clock:
        clock.setattr("hoh_control_plane.service.planner_context._iso", lambda: "2000-01-01T00:00:00Z")
        old = service.append_planner_context(actors["planner"], **args, source_ref="evidence://before")
    queued = service.queue_project_tests(actors["qa"], t["ticket_id"], claim["lease"]["lease_id"])
    run_id = queued["test_run"]["test_run_id"]
    service.fail_queued_project_test(run_id, "Disposable fixture worker interruption")
    blocked = service.get_ticket_context(actors["planner"], t["ticket_id"])["ticket"]
    with pytest.raises(ValidationError, match="readiness"):
        service.recover_candidate_for_qa(actors["planner"], t["ticket_id"], blocked["version"], run_id, old["entry_id"])
    fresh = service.append_planner_context(actors["planner"], **args, source_ref="evidence://after")
    recovered = service.recover_candidate_for_qa(actors["planner"], t["ticket_id"], blocked["version"], run_id, fresh["entry_id"])
    assert recovered["ticket"]["status"] == "QA_READY"
    assert recovered["candidate"]["candidate_id"] == submitted["candidate"]["candidate_id"]
    with service.store.connection() as db:
        candidate = service._one(db, "SELECT * FROM candidates WHERE candidate_id=?", (submitted["candidate"]["candidate_id"],))
        assert candidate["qa_review_plan"] == submitted["candidate"]["qa_review_plan"]
        assert db.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] == 1


def test_metrics_preserve_history_without_double_counting_aliases(service, actors, seeded):
    ticket_id = seeded["ticket"]["ticket_id"]
    submitted = submit_fake(service, actors, ticket_id)
    verdict(service, actors, ticket_id, "passed", [finding(c) for c in seeded["ticket"]["acceptance_criteria"]])
    with service.store.connection(immediate=True) as db:
        service._event(db, actors["qa"], "project-1", "qa.verdict_passed", "ticket", ticket_id, 4,
                       {"candidate_id": submitted["candidate"]["candidate_id"]})
        service._event(db, actors["qa"], "project-1", "qa.verdict_failed", "ticket", "legacy", 1, {})
        service._event(db, actors["qa"], "project-1", "qa.infrastructure_blocked", "ticket", "infra", 1,
                       {"reason": "worker_failure"})
    actual = WorkflowMetrics(service.store).project_metrics(actors["planner"], "project-1")["rework"]
    assert actual["qa_passes"] == 1 and actual["qa_failures"] == 1
    assert actual["candidate_pass_rate"] == 0.5
