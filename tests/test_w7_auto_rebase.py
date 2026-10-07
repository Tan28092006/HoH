"""Wave 7B edge cases: a drifted queued candidate is rebased by the server.

Lifecycle-driven: a real repository, a real candidate through QA, a real
teammate commit on main, and the ordinary sync so HoH has observed it.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from helpers.barrier import keep_open_tickets
from helpers.w7 import (
    claim_integration,
    commit_file,
    events,
    finding,
    git,
    queue_merge,
    reconciliations,
    repository,
    ticket,
    verified_with_repository,
)

from hoh_control_plane.domain import ValidationError
from hoh_control_plane.git_worktrees import GitWorktreeManager
from hoh_control_plane.service._common import MAX_AUTO_REBASES
from hoh_control_plane.service.run_guardrails import RED_MAIN_PAUSE


def _observed_project(service, actors, tmp_path: Path) -> Path:
    repo = repository(tmp_path)
    service.configure_repository(actors["planner"], "project-1", str(repo))
    from helpers.w7 import REGRESSION_COMMAND

    service.configure_test_command(actors["planner"], "project-1", REGRESSION_COMMAND, 60)
    service.sync_repository(actors["planner"], "project-1")
    keep_open_tickets(service, actors["planner"])
    return repo


def _queued(service, actors, seeded, tmp_path: Path, **kwargs):
    repo = _observed_project(service, actors, tmp_path)
    ready = verified_with_repository(service, actors, seeded, tmp_path, repo=repo, **kwargs)
    integration = claim_integration(service, actors, "DEV-142")
    queued = queue_merge(service, actors, "DEV-142", integration)
    return repo, ready, integration, queued["merge_job"]["merge_job_id"]


def _teammate(service, actors, repo: Path, relative: str, text: str = "teammate\n",
              observe: bool = True) -> str:
    sha = commit_file(repo, relative, text, f"Teammate changes {relative}")
    if observe:
        service.sync_repository(actors["planner"], "project-1")
        # A drift that reaches the ticket freezes it until Planner keeps it.
        keep_open_tickets(service, actors["planner"])
    return sha


def _candidates(service) -> list[dict]:
    with service.store.connection() as connection:
        return [dict(row) for row in connection.execute(
            "SELECT * FROM candidates WHERE ticket_id = 'DEV-142' ORDER BY submitted_at"
        ).fetchall()]


def _job(service, merge_job_id: str) -> dict:
    with service.store.connection() as connection:
        return service._merge_job(connection, merge_job_id)


# ------------------------------------------------------------- the paths


def test_unobserved_drift_keeps_the_old_rework_path(service, actors, seeded, tmp_path) -> None:
    repo, _ready, _integration, job_id = _queued(service, actors, seeded, tmp_path)
    _teammate(service, actors, repo, "src/other/x.txt", observe=False)

    job = service.execute_queued_merge(job_id)

    assert job["merge_status"] == "base_drift"
    assert ticket(service, "DEV-142")["status"] == "REWORK"
    assert len(_candidates(service)) == 1, "no derived candidate for an unobserved main"
    assert events(service, "candidate.rebased") == []


def test_disjoint_drift_reuses_qa_and_lands_by_compare_and_set(
    service, actors, seeded, tmp_path
) -> None:
    repo, ready, integration, job_id = _queued(service, actors, seeded, tmp_path)
    moved = _teammate(service, actors, repo, "src/other/x.txt")

    requeued = service.execute_queued_merge(job_id)

    original, derived = _candidates(service)
    assert requeued["status"] == "QUEUED"
    assert requeued["candidate_id"] == derived["candidate_id"]
    assert derived["derived_from"] == original["candidate_id"]
    assert derived["qa_evidence_from"] == original["candidate_id"]
    assert derived["base_commit"] == moved
    assert git(repo, "rev-parse", f"{derived['commit_sha']}^") == moved
    assert git(repo, "rev-parse", f"refs/hoh/rebased/DEV-142/{original['candidate_id']}-r1") \
        == derived["commit_sha"]
    current = ticket(service, "DEV-142")
    assert current["status"] == "MERGE_READY"
    assert current["candidate_commit"] == derived["commit_sha"]
    assert requeued["expected_version"] == current["version"]
    [rebased] = events(service, "candidate.rebased")
    assert rebased["payload"]["qa_reused"] is True
    assert rebased["payload"]["auto_rebase"] == 1
    # The same lease still covers the job.
    with service.store.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM leases WHERE lease_id = ?", (integration["lease"]["lease_id"],)
        ).fetchone()[0] == 1

    landed = service.execute_queued_merge(job_id)

    assert landed["status"] == "COMPLETED"
    assert landed["merge_status"] == "merged"
    assert git(repo, "rev-parse", "main") == derived["commit_sha"]
    assert git(repo, "status", "--porcelain") == ""
    assert (repo / "src/checkout/retry.txt").read_text(encoding="utf-8") == "safe retry\n"
    assert ticket(service, "DEV-142")["status"] == "DONE"
    [gate] = events(service, "integration.gate_passed", job_id)
    assert gate["payload"]["gate_mode"] == "executed_rebased"
    assert gate["payload"]["candidate_id"] == derived["candidate_id"]
    with service.store.connection() as connection:
        runs = connection.execute(
            "SELECT exit_code FROM qa_test_runs WHERE candidate_id = ?", (derived["candidate_id"],)
        ).fetchall()
    assert [row["exit_code"] for row in runs] == [0], "the gate ran the suite on the rebased tree"


def test_overlapping_drift_sends_the_derived_candidate_to_qa(service, actors, seeded, tmp_path) -> None:
    repo, _ready, integration, job_id = _queued(service, actors, seeded, tmp_path)
    _teammate(service, actors, repo, "src/checkout/other.txt")

    job = service.execute_queued_merge(job_id)

    original, derived = _candidates(service)
    assert job["status"] == "COMPLETED"
    assert job["merge_status"] == "base_drift"
    assert derived["qa_evidence_from"] is None
    current = ticket(service, "DEV-142")
    assert current["status"] == "QA_READY"
    assert current["candidate_commit"] == derived["commit_sha"]
    with service.store.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM leases WHERE lease_id = ?", (integration["lease"]["lease_id"],)
        ).fetchone()[0] == 0
    assert events(service, "candidate.rebased")[0]["payload"]["qa_reused"] is False


@pytest.mark.parametrize("path", ["README.md", "docs/guide.md"])
def test_a_root_or_docs_change_on_main_never_carries_qa(service, actors, seeded, tmp_path, path) -> None:
    repo, _ready, _integration, job_id = _queued(service, actors, seeded, tmp_path)
    _teammate(service, actors, repo, path, "changed\n")

    job = service.execute_queued_merge(job_id)

    assert job["merge_status"] == "base_drift"
    assert ticket(service, "DEV-142")["status"] == "QA_READY"


def test_a_conflicting_drift_goes_to_rework(service, actors, seeded, tmp_path) -> None:
    repo, _ready, _integration, job_id = _queued(service, actors, seeded, tmp_path)
    _teammate(service, actors, repo, "src/checkout/retry.txt", "a different retry\n")

    job = service.execute_queued_merge(job_id)

    assert job["merge_status"] == "base_drift"
    current = ticket(service, "DEV-142")
    assert current["status"] == "REWORK"
    assert "does not apply cleanly" in current["blocked_reason"]
    assert len(_candidates(service)) == 1


def test_a_drift_that_already_contains_the_change_is_not_a_rebase(
    service, actors, seeded, tmp_path
) -> None:
    repo, _ready, _integration, job_id = _queued(service, actors, seeded, tmp_path)
    # A teammate applied the same change by hand: the replay is empty.
    _teammate(service, actors, repo, "src/checkout/retry.txt", "safe retry\n")

    job = service.execute_queued_merge(job_id)

    assert job["merge_status"] == "base_drift"
    assert ticket(service, "DEV-142")["status"] == "REWORK"
    assert len(_candidates(service)) == 1


def test_the_rebase_budget_allows_the_third_and_blocks_the_fourth(
    service, actors, seeded, tmp_path
) -> None:
    repo, _ready, _integration, job_id = _queued(service, actors, seeded, tmp_path)
    for attempt in range(1, MAX_AUTO_REBASES + 1):
        _teammate(service, actors, repo, f"src/other/x{attempt}.txt")
        job = service.execute_queued_merge(job_id)
        assert job["status"] == "QUEUED", attempt
        assert len(_candidates(service)) == attempt + 1
    assert events(service, "candidate.rebased")[-1]["payload"]["auto_rebase"] == MAX_AUTO_REBASES

    _teammate(service, actors, repo, "src/other/one-too-many.txt")
    job = service.execute_queued_merge(job_id)

    assert job["merge_status"] == "base_drift"
    current = ticket(service, "DEV-142")
    assert current["status"] == "BLOCKED"
    assert current["blocked_reason"].startswith("drift_budget_exhausted")
    assert len(_candidates(service)) == MAX_AUTO_REBASES + 1
    with service.store.connection() as connection:
        jobs = connection.execute(
            "SELECT job_type, status FROM planner_jobs WHERE ticket_id = 'DEV-142'"
        ).fetchall()
    assert any(row["status"] != "RESOLVED" for row in jobs)


def test_a_ticket_that_moves_during_the_rebase_fails_the_job(
    service, actors, seeded, tmp_path, monkeypatch
) -> None:
    repo, _ready, _integration, job_id = _queued(service, actors, seeded, tmp_path)
    _teammate(service, actors, repo, "src/other/x.txt")
    original = service.git_worktrees.rebase_candidate

    def racing_rebase(**kwargs):
        rebased = original(**kwargs)
        with service.store.connection(immediate=True) as connection:
            connection.execute("UPDATE tickets SET version = version + 1 WHERE ticket_id = 'DEV-142'")
        return rebased

    monkeypatch.setattr(service.git_worktrees, "rebase_candidate", racing_rebase)

    job = service.execute_queued_merge(job_id)

    assert job["status"] == "FAILED"
    assert "changed while the candidate was rebased" in job["error_message"]
    assert len(_candidates(service)) == 1, "no derived candidate is recorded"


def test_a_failing_gate_on_the_rebased_tree_lands_nothing(service, actors, seeded, tmp_path) -> None:
    # The suite fails only when the teammate's file is present, so the original
    # candidate's QA run passed and only the rebased tree is red.
    command = ["python", "-c",
               "import os, sys; sys.exit(1 if os.path.exists('src/other/x.txt') else 0)"]
    repo = _observed_project(service, actors, tmp_path)
    service.configure_test_command(actors["planner"], "project-1", command, 60)
    ready = verified_with_repository(service, actors, seeded, tmp_path, repo=repo)
    _seed_run(service, ready["candidate"]["candidate_id"], command)
    integration = claim_integration(service, actors, "DEV-142")
    job_id = queue_merge(service, actors, "DEV-142", integration)["merge_job"]["merge_job_id"]
    main_before = _teammate(service, actors, repo, "src/other/x.txt")

    assert service.execute_queued_merge(job_id)["status"] == "QUEUED"
    gated = service.execute_queued_merge(job_id)

    assert gated["status"] == "FAILED"
    assert git(repo, "rev-parse", "main") == main_before
    assert ticket(service, "DEV-142")["status"] == "REWORK"


def _seed_run(service, candidate_id: str, command: list[str]) -> None:
    import hashlib

    digest = hashlib.sha256("\0".join(command).encode()).hexdigest()
    with service.store.connection(immediate=True) as connection:
        connection.execute(
            """INSERT INTO qa_test_runs
               (test_run_id, candidate_id, ticket_id, command_digest, exit_code, timed_out,
                duration_ms, storage_ref, report_path, executed_by, created_at,
                termination_reason)
               VALUES (?, ?, 'DEV-142', ?, 0, 0, 10, 'evidence://local/run.json', 'run.json',
                       'qa-01', '2026-01-01T00:00:00Z', 'passed')""",
            (f"testrun_custom_{candidate_id[-8:]}", candidate_id, digest),
        )


def test_the_regression_waiver_is_only_for_a_derived_candidate_before_its_gate(
    service, actors, seeded, tmp_path
) -> None:
    repo, ready, _integration, job_id = _queued(service, actors, seeded, tmp_path)
    _teammate(service, actors, repo, "src/other/x.txt")
    service.execute_queued_merge(job_id)
    snapshot = service._release_policy_snapshot(actors["integrator"], "DEV-142")
    derived = snapshot["candidate"]

    assert derived["derived_from"] == ready["candidate"]["candidate_id"]
    # QA evidence is read from the candidate it was rebased from.
    assert snapshot["qa_verified"] is True
    assert snapshot["findings"], "findings come from qa_evidence_from"
    # Before its gate the derived candidate has no run of its own: waived ...
    service._require_release_evidence(snapshot, require_regression_run=False)
    # ... but not by default, which is what the post-gate check uses.
    from hoh_control_plane.domain import ReleasePolicyError

    with pytest.raises(ReleasePolicyError) as refused:
        service._require_release_evidence(snapshot)
    assert refused.value.details["reason"] == "regression_run_missing"


def test_a_paused_queue_holds_a_requeued_rebase(service, actors, seeded, tmp_path) -> None:
    repo, _ready, _integration, job_id = _queued(service, actors, seeded, tmp_path)
    _teammate(service, actors, repo, "src/other/x.txt")
    assert service.execute_queued_merge(job_id)["status"] == "QUEUED"
    main_before = git(repo, "rev-parse", "main")
    with service.store.connection(immediate=True) as connection:
        service._pause_merge_queue(
            connection, "project-1", pause_reason=RED_MAIN_PAUSE, detail="red main",
            ticket=None, evidence_refs=[],
        )

    held = service.execute_queued_merge(job_id)

    assert held["status"] == "QUEUED"
    assert held["started_at"] is None
    assert git(repo, "rev-parse", "main") == main_before
    service.resume_merge_queue(actors["planner"], "project-1", "Main is green again")
    assert service.execute_queued_merge(job_id)["merge_status"] == "merged"


def test_the_derived_candidate_keeps_the_maker_family(service, actors, seeded, tmp_path) -> None:
    service.register_agent(actors["developer"], skills=["hoh-developer"], model_family="anthropic")
    service.register_agent(actors["qa"], skills=["hoh-qa"], model_family="openai")
    service.register_agent(actors["integrator"], skills=["hoh-integrator"], model_family="openai")
    repo, _ready, _integration, job_id = _queued(service, actors, seeded, tmp_path)
    _teammate(service, actors, repo, "src/checkout/other.txt")

    service.execute_queued_merge(job_id)

    _original, derived = _candidates(service)
    assert derived["maker_model_family"] == "anthropic"
    # An OpenAI QA may check the derived candidate; an Anthropic one may not.
    claim = service.claim_qa(actors["qa"], "DEV-142", ticket(service, "DEV-142")["version"], 900)
    assert claim["family_check"]["status"] == "distinct"


def test_a_high_risk_derived_candidate_sent_to_qa_needs_a_fresh_sign_off(
    service, actors, seeded, tmp_path
) -> None:
    from hoh_control_plane.domain import ReleasePolicyError
    from helpers.w7 import pass_all, record_regression_run

    service.configure_risk_policy(actors["planner"], "project-1",
                                  high_risk_patterns=["src/checkout/**"])
    review = finding("adversarial review", evidence_type="adversarial_review",
                     storage_ref="evidence://qa/review.json")
    repo = _observed_project(service, actors, tmp_path)
    verified_with_repository(service, actors, seeded, tmp_path, repo=repo, extra_findings=[review])
    service.approve_high_risk(actors["planner"], "DEV-142", "Approved the original")
    integration = claim_integration(service, actors, "DEV-142")
    job_id = queue_merge(service, actors, "DEV-142", integration)["merge_job"]["merge_job_id"]
    _teammate(service, actors, repo, "src/checkout/other.txt")
    service.execute_queued_merge(job_id)
    _original, derived = _candidates(service)

    pass_all(service, actors, "DEV-142", extra=[review])
    record_regression_run(service, derived["candidate_id"], "DEV-142")
    again = claim_integration(service, actors, "DEV-142")

    with pytest.raises(ReleasePolicyError) as refused:
        queue_merge(service, actors, "DEV-142", again)
    assert refused.value.details["reason"] == "high_risk_signoff_missing"
    service.approve_high_risk(actors["planner"], "DEV-142", "Approved the rebased tree")
    assert queue_merge(service, actors, "DEV-142", again)["merge_job"]["status"] == "QUEUED"


def test_a_high_risk_derived_candidate_with_reused_qa_lands_on_the_original_sign_off(
    service, actors, seeded, tmp_path
) -> None:
    """Pinned behaviour: the carried job is not re-gated by the risk tier.

    The patch is byte-identical and main's change is disjoint, so the sign-off
    given for the original candidate lands the derived one in the same job.
    """
    service.configure_risk_policy(actors["planner"], "project-1",
                                  high_risk_patterns=["src/checkout/**"])
    review = finding("adversarial review", evidence_type="adversarial_review",
                     storage_ref="evidence://qa/review.json")
    repo = _observed_project(service, actors, tmp_path)
    verified_with_repository(service, actors, seeded, tmp_path, repo=repo, extra_findings=[review])
    service.approve_high_risk(actors["planner"], "DEV-142", "Approved the original")
    integration = claim_integration(service, actors, "DEV-142")
    job_id = queue_merge(service, actors, "DEV-142", integration)["merge_job"]["merge_job_id"]
    _teammate(service, actors, repo, "src/other/x.txt")

    assert service.execute_queued_merge(job_id)["status"] == "QUEUED"
    assert service.execute_queued_merge(job_id)["merge_status"] == "merged"


# --------------------------------------------------------------- Git half


def _git_setup(tmp_path: Path):
    repo = repository(tmp_path, "git-half")
    base = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "-b", "candidate")
    git(repo, "config", "user.name", "Original Author")
    candidate = commit_file(repo, "src/a/x.txt", "candidate\n", "Candidate message")
    git(repo, "config", "user.name", "HoH Tests")
    git(repo, "checkout", "-q", "main")
    return repo, base, candidate, GitWorktreeManager(tmp_path / "worktrees")


def test_rebase_keeps_author_and_message_and_leaves_the_checkout_alone(tmp_path) -> None:
    repo, _base, candidate, manager = _git_setup(tmp_path)
    moved = commit_file(repo, "src/b/y.txt", "b\n")
    head_before = git(repo, "rev-parse", "HEAD")

    rebased = manager.rebase_candidate(
        repository_path=str(repo), project_id="p1", ticket_id="t1",
        candidate_commit=candidate, onto_commit=moved, keep_ref="refs/hoh/rebased/t1/c1")

    assert git(repo, "log", "-1", "--format=%an|%s", rebased) == "Original Author|Candidate message"
    assert git(repo, "rev-parse", "HEAD") == head_before
    assert git(repo, "status", "--porcelain") == ""
    assert not (repo / "src/a/x.txt").exists(), "the shared checkout is not touched"


def test_rebase_of_an_already_applied_change_is_none(tmp_path) -> None:
    repo, _base, candidate, manager = _git_setup(tmp_path)
    moved = commit_file(repo, "src/a/x.txt", "candidate\n", "Same change by hand")

    assert manager.rebase_candidate(
        repository_path=str(repo), project_id="p1", ticket_id="t1",
        candidate_commit=candidate, onto_commit=moved, keep_ref="refs/hoh/rebased/t1/c1") is None
    assert subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "-q",
                           "refs/hoh/rebased/t1/c1"], capture_output=True).returncode != 0


def test_rebase_of_an_unknown_commit_is_refused(tmp_path) -> None:
    repo, _base, _candidate, manager = _git_setup(tmp_path)
    with pytest.raises(ValidationError):
        manager.rebase_candidate(
            repository_path=str(repo), project_id="p1", ticket_id="t1",
            candidate_commit="0" * 40, onto_commit="main", keep_ref="refs/hoh/rebased/t1/c1")


def test_cas_refuses_a_dirty_checked_out_branch(tmp_path) -> None:
    repo, _base, candidate, manager = _git_setup(tmp_path)
    moved = commit_file(repo, "src/b/y.txt", "b\n")
    rebased = manager.rebase_candidate(
        repository_path=str(repo), project_id="p1", ticket_id="t1",
        candidate_commit=candidate, onto_commit=moved, keep_ref="refs/hoh/rebased/t1/c1")
    (repo / "README.md").write_text("uncommitted\n", encoding="utf-8")

    with pytest.raises(ValidationError, match="clean"):
        manager.cas_fast_forward(repository_path=str(repo), default_branch="main",
                                 expected_old_commit=moved, new_commit=rebased)
    assert git(repo, "rev-parse", "main") == moved
    assert (repo / "README.md").read_text(encoding="utf-8") == "uncommitted\n"


def test_cas_refuses_a_commit_that_does_not_descend_from_the_old_value(tmp_path) -> None:
    repo, base, candidate, manager = _git_setup(tmp_path)
    moved = commit_file(repo, "src/b/y.txt", "b\n")
    with pytest.raises(ValidationError, match="descend"):
        manager.cas_fast_forward(repository_path=str(repo), default_branch="main",
                                 expected_old_commit=moved, new_commit=candidate)
    assert git(repo, "rev-parse", "main") == moved
    assert base != moved


def test_cas_moves_only_the_ref_when_the_branch_is_not_checked_out(tmp_path) -> None:
    repo, _base, candidate, manager = _git_setup(tmp_path)
    moved = commit_file(repo, "src/b/y.txt", "b\n")
    rebased = manager.rebase_candidate(
        repository_path=str(repo), project_id="p1", ticket_id="t1",
        candidate_commit=candidate, onto_commit=moved, keep_ref="refs/hoh/rebased/t1/c1")
    git(repo, "checkout", "-q", "--detach", moved)
    (repo / "scratch.txt").write_text("detached work\n", encoding="utf-8")

    merged, head = manager.cas_fast_forward(repository_path=str(repo), default_branch="main",
                                            expected_old_commit=moved, new_commit=rebased)

    assert (merged, head) == (True, rebased)
    assert git(repo, "rev-parse", "main") == rebased
    assert git(repo, "rev-parse", "HEAD") == moved
    assert (repo / "scratch.txt").exists()
