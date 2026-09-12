"""A candidate from a workspace the service never held (HOH-245).

The Developer keeps its own checkout on its own machine and pushes the branch to
the repository the service can read. Every identity fact is then derived by the
service from what it fetched, never from what the agent said about its machine.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hoh_control_plane.domain import Role, ValidationError, WorkspaceModel


def _git(path: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(path), *arguments], check=True, capture_output=True, text=True
    )
    return completed.stdout.strip()


def _service_repository(tmp_path: Path) -> Path:
    """The repository the service reads. Bare, because nobody works in it."""
    repository = tmp_path / "service-repository"
    repository.mkdir(parents=True)
    _git(repository, "init", "--initial-branch=main")
    _git(repository, "config", "user.email", "hoh-tests@example.invalid")
    _git(repository, "config", "user.name", "HoH Tests")
    _git(repository, "config", "receive.denyCurrentBranch", "ignore")
    (repository / "README.md").write_text("# Checkout\n", encoding="utf-8")
    _git(repository, "add", "README.md")
    _git(repository, "commit", "-m", "Initial checkout")
    return repository


def _agent_clone(tmp_path: Path, service_repository: Path) -> Path:
    """The Developer's own checkout, on its own machine."""
    clone = tmp_path / "agent-machine"
    subprocess.run(
        ["git", "clone", str(service_repository), str(clone)],
        check=True, capture_output=True, text=True,
    )
    _git(clone, "config", "user.email", "agent@example.invalid")
    _git(clone, "config", "user.name", "Remote Developer")
    return clone


def _commit_in_agent_clone(clone: Path, relative: str, body: str, message: str) -> str:
    target = clone / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    _git(clone, "add", relative)
    _git(clone, "commit", "-m", message)
    return _git(clone, "rev-parse", "HEAD")


@pytest.fixture
def remote(service, actors, seeded, tmp_path: Path):
    """A project on the agent-workspace model, and a Developer holding its own clone."""
    repository = _service_repository(tmp_path)
    service.configure_repository(
        actors["planner"],
        "project-1",
        str(repository),
        WorkspaceModel.AGENT_WORKSPACE.value,
    )
    clone = _agent_clone(tmp_path, repository)
    ticket = seeded["ticket"]
    claim = service.claim_ticket(
        actors["developer"], ticket["ticket_id"], ticket["version"], 900
    )
    base = _git(clone, "rev-parse", "HEAD")
    return {
        "repository": repository,
        "clone": clone,
        "ticket": ticket,
        "lease_id": claim["lease"]["lease_id"],
        "version": claim["ticket"]["version"],
        "base": base,
        "branch": "agent/DEV-142",
    }


def _submit(service, actors, remote, commit: str, base: str | None = None):
    return service.submit_candidate(
        actors["developer"],
        remote["ticket"]["ticket_id"],
        remote["lease_id"],
        remote["version"],
        commit,
        base or remote["base"],
        remote["branch"],
        ["Implemented on my own machine and pushed the branch."],
    )


def test_a_pushed_candidate_is_accepted_without_the_service_holding_a_worktree(
    service, actors, remote
) -> None:
    _git(remote["clone"], "checkout", "-b", remote["branch"])
    commit = _commit_in_agent_clone(
        remote["clone"], "src/checkout/retry.py", "def retry():\n    ...\n", "Add retry"
    )
    _git(remote["clone"], "push", "origin", remote["branch"])

    result = _submit(service, actors, remote, commit)

    assert result["candidate"]["commit_sha"] == commit
    assert result["candidate"]["base_commit"] == remote["base"]
    assert result["ticket"]["status"] == "QA_READY"


def test_a_commit_the_service_cannot_resolve_is_refused(service, actors, remote) -> None:
    """The agent's word about its own machine is never evidence."""
    _git(remote["clone"], "checkout", "-b", remote["branch"])
    commit = _commit_in_agent_clone(
        remote["clone"], "src/checkout/retry.py", "def retry():\n    ...\n", "Add retry"
    )
    # Deliberately not pushed: it exists only on the agent's machine.

    with pytest.raises(ValidationError) as error:
        _submit(service, actors, remote, commit)

    assert "cannot resolve" in str(error.value).lower()


def test_remote_candidate_can_run_qa_on_the_service_without_a_developer_worktree(service, actors, remote):
    import json
    import sys
    service.configure_test_command(actors["planner"], "project-1", [sys.executable, "-c",
        "from pathlib import Path; assert Path('src/checkout/retry.py').read_text() == 'verified\\n'"], 30)
    _git(remote["clone"], "checkout", "-b", remote["branch"])
    commit = _commit_in_agent_clone(remote["clone"], "src/checkout/retry.py", "verified\n", "Candidate")
    _git(remote["clone"], "push", "origin", remote["branch"])
    candidate = _submit(service, actors, remote, commit)
    claim = service.claim_qa(actors["qa"], remote["ticket"]["ticket_id"], candidate["ticket"]["version"], 900)
    queued = service.queue_project_tests(actors["qa"], remote["ticket"]["ticket_id"], claim["lease"]["lease_id"])
    result = service.execute_queued_project_test(queued["test_run"]["test_run_id"])
    assert result["status"] == "COMPLETED", result
    context = service.get_ticket_context(actors["qa"], remote["ticket"]["ticket_id"])
    assert context["test_runs"][0]["exit_code"] == 0
    assert not Path(queued["test_run"]["worktree_path"]).exists()
    assert context["worktree"] is None


def test_a_candidate_that_does_not_descend_from_its_claimed_base_is_refused(
    service, actors, remote
) -> None:
    _git(remote["clone"], "checkout", "-b", remote["branch"])
    commit = _commit_in_agent_clone(
        remote["clone"], "src/checkout/retry.py", "x = 1\n", "Add retry"
    )
    _git(remote["clone"], "push", "origin", remote["branch"])
    # An unrelated root commit, so the claimed base is not an ancestor.
    _git(remote["clone"], "checkout", "--orphan", "elsewhere")
    unrelated = _commit_in_agent_clone(
        remote["clone"], "other.txt", "unrelated\n", "Unrelated root"
    )
    _git(remote["clone"], "push", "origin", "elsewhere")

    with pytest.raises(ValidationError) as error:
        _submit(service, actors, remote, commit, base=unrelated)

    assert "descend" in str(error.value).lower()


def test_changed_paths_are_derived_by_the_service_not_supplied_by_the_agent(
    service, actors, remote
) -> None:
    """Write scope is enforced against what the service can see in the commit."""
    _git(remote["clone"], "checkout", "-b", remote["branch"])
    commit = _commit_in_agent_clone(
        remote["clone"], "src/elsewhere/forbidden.py", "x = 1\n", "Touch another area"
    )
    _git(remote["clone"], "push", "origin", remote["branch"])

    with pytest.raises(ValidationError) as error:
        _submit(service, actors, remote, commit)

    assert "write scope" in str(error.value).lower()
    assert "src/elsewhere/forbidden.py" in str(error.value)


def test_a_frozen_candidate_survives_the_agent_rewriting_its_branch(
    service, actors, remote
) -> None:
    """Identity comes from what the service fetched, not from the agent's branch."""
    _git(remote["clone"], "checkout", "-b", remote["branch"])
    commit = _commit_in_agent_clone(
        remote["clone"], "src/checkout/retry.py", "def retry():\n    ...\n", "Add retry"
    )
    _git(remote["clone"], "push", "origin", remote["branch"])
    frozen = _submit(service, actors, remote, commit)["candidate"]

    _git(remote["clone"], "commit", "--amend", "-m", "Rewritten history")
    _git(remote["clone"], "push", "--force", "origin", remote["branch"])

    with service.store.connection() as connection:
        stored = dict(
            connection.execute(
                "SELECT * FROM candidates WHERE candidate_id = ?",
                (frozen["candidate_id"],),
            ).fetchone()
        )
    assert stored["commit_sha"] == commit, "the frozen candidate keeps its identity"


def test_the_service_hands_back_no_path_of_its_own(service, actors, remote) -> None:
    _git(remote["clone"], "checkout", "-b", remote["branch"])
    commit = _commit_in_agent_clone(
        remote["clone"], "src/checkout/retry.py", "x = 1\n", "Add retry"
    )
    _git(remote["clone"], "push", "origin", remote["branch"])

    result = _submit(service, actors, remote, commit)

    assert "worktree" not in result or result.get("worktree") is None
    with service.store.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM worktrees WHERE ticket_id = ?",
            (remote["ticket"]["ticket_id"],),
        ).fetchone()[0] == 0


def test_a_colocated_project_still_requires_its_assigned_worktree(
    service, actors, seeded, tmp_path: Path
) -> None:
    """The other model is unchanged: this adds a path rather than replacing one."""
    repository = _service_repository(tmp_path)
    service.configure_repository(actors["planner"], "project-1", str(repository))
    ticket = seeded["ticket"]
    claim = service.claim_ticket(
        actors["developer"], ticket["ticket_id"], ticket["version"], 900
    )

    with pytest.raises(ValidationError) as error:
        service.submit_candidate(
            actors["developer"],
            ticket["ticket_id"],
            claim["lease"]["lease_id"],
            claim["ticket"]["version"],
            "0" * 40,
            "1" * 40,
            "some/branch",
            ["No worktree was ever prepared."],
        )

    assert "worktree" in str(error.value).lower()


def test_qa_and_integration_see_an_ordinary_frozen_candidate(
    service, actors, remote
) -> None:
    """Once frozen, nothing downstream can tell which model produced it."""
    _git(remote["clone"], "checkout", "-b", remote["branch"])
    commit = _commit_in_agent_clone(
        remote["clone"], "src/checkout/retry.py", "x = 1\n", "Add retry"
    )
    _git(remote["clone"], "push", "origin", remote["branch"])
    submitted = _submit(service, actors, remote, commit)

    qa_claim = service.claim_qa(
        actors["qa"], remote["ticket"]["ticket_id"], submitted["ticket"]["version"], 900
    )

    assert qa_claim["ticket"]["status"] == "QA_IN_PROGRESS"
    assert qa_claim["ticket"]["candidate_commit"] == commit
