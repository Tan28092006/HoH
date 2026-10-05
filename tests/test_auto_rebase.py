"""Git half of the wave 7B auto-rebase: replay onto a moved main, and CAS landing."""
from __future__ import annotations

import subprocess
from pathlib import Path

from hoh_control_plane.git_worktrees import GitWorktreeManager


def _git(path: Path, *arguments: str) -> str:
    return subprocess.run(["git", "-C", str(path), *arguments], check=True,
                          capture_output=True, text=True).stdout.strip()


def _commit(repository: Path, name: str, text: str, message: str) -> str:
    (repository / name).parent.mkdir(parents=True, exist_ok=True)
    (repository / name).write_text(text, encoding="utf-8")
    _git(repository, "add", name)
    _git(repository, "commit", "-m", message)
    return _git(repository, "rev-parse", "HEAD")


def _setup(tmp_path: Path):
    repository = tmp_path / "repo"
    repository.mkdir()
    _git(repository, "init", "--initial-branch=main")
    _git(repository, "config", "user.email", "hoh-tests@example.invalid")
    _git(repository, "config", "user.name", "HoH Tests")
    base = _commit(repository, "src/a/x.txt", "a\n", "base")
    _git(repository, "checkout", "-q", "-b", "candidate")
    candidate = _commit(repository, "src/a/x.txt", "a changed\n", "candidate")
    _git(repository, "checkout", "-q", "main")
    return repository, base, candidate, GitWorktreeManager(tmp_path / "worktrees")


def test_disjoint_drift_rebases_and_lands_by_compare_and_set(tmp_path: Path) -> None:
    repository, _base, candidate, manager = _setup(tmp_path)
    moved = _commit(repository, "src/b/y.txt", "b\n", "other work")
    rebased = manager.rebase_candidate(
        repository_path=str(repository), project_id="p1", ticket_id="t1",
        candidate_commit=candidate, onto_commit=moved, keep_ref="refs/hoh/rebased/t1/c1")
    assert rebased is not None
    assert _git(repository, "rev-parse", f"{rebased}^") == moved
    assert _git(repository, "rev-parse", "refs/hoh/rebased/t1/c1") == rebased
    merged, head = manager.cas_fast_forward(
        repository_path=str(repository), default_branch="main",
        expected_old_commit=moved, new_commit=rebased)
    assert (merged, head) == (True, rebased)
    assert (repository / "src/a/x.txt").read_text(encoding="utf-8") == "a changed\n"
    assert _git(repository, "status", "--porcelain") == ""


def test_conflicting_drift_returns_no_rebased_commit(tmp_path: Path) -> None:
    repository, _base, candidate, manager = _setup(tmp_path)
    moved = _commit(repository, "src/a/x.txt", "a conflicting\n", "conflict")
    assert manager.rebase_candidate(
        repository_path=str(repository), project_id="p1", ticket_id="t1",
        candidate_commit=candidate, onto_commit=moved, keep_ref="refs/hoh/rebased/t1/c1") is None


def test_compare_and_set_refuses_a_commit_that_landed_in_between(tmp_path: Path) -> None:
    repository, _base, candidate, manager = _setup(tmp_path)
    moved = _commit(repository, "src/b/y.txt", "b\n", "other work")
    rebased = manager.rebase_candidate(
        repository_path=str(repository), project_id="p1", ticket_id="t1",
        candidate_commit=candidate, onto_commit=moved, keep_ref="refs/hoh/rebased/t1/c1")
    racer = _commit(repository, "src/c/z.txt", "c\n", "raced in")
    merged, head = manager.cas_fast_forward(
        repository_path=str(repository), default_branch="main",
        expected_old_commit=moved, new_commit=rebased)
    assert (merged, head) == (False, racer)
    assert _git(repository, "rev-parse", "main") == racer
