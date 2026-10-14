"""R6/G14: moving the shared checkout's branch is one side effect that finishes.

The ref moves by compare-and-set under the repository lock, then the working
tree follows. A slow Git, a supervisor stop or a lapsed lease must not leave the
ref at one commit and the tree at another; a move an earlier process left half
done is finished, and a stale index.lock is cleared only when no live Git can
own it.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hoh_control_plane import git_worktrees
from hoh_control_plane.domain import ValidationError
from hoh_control_plane.git_worktrees import FAST_FORWARD_MARKER, GitWorktreeManager
from hoh_control_plane.managed_process import run_managed


def _git(path: Path, *arguments: str) -> str:
    return subprocess.run(["git", "-C", str(path), *arguments], check=True,
                          capture_output=True, text=True).stdout.strip()


def _repository(tmp_path: Path) -> tuple[Path, str, str]:
    """A repository on main at base, plus a candidate commit one step ahead."""
    repository = tmp_path / "shared"
    repository.mkdir()
    _git(repository, "init", "--initial-branch=main")
    _git(repository, "config", "user.email", "hoh-tests@example.invalid")
    _git(repository, "config", "user.name", "HoH Tests")
    _git(repository, "config", "core.autocrlf", "false")
    (repository / "README.md").write_text("# project\n", encoding="utf-8")
    (repository / "gone.txt").write_text("deleted by the candidate\n", encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "base")
    base = _git(repository, "rev-parse", "HEAD")
    _git(repository, "checkout", "-q", "-b", "work")
    (repository / "src").mkdir()
    (repository / "src" / "feature.txt").write_text("candidate change\n", encoding="utf-8")
    (repository / "README.md").write_text("# project, edited\n", encoding="utf-8")
    _git(repository, "rm", "-q", "gone.txt")
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "candidate")
    candidate = _git(repository, "rev-parse", "HEAD")
    _git(repository, "checkout", "-q", "main")
    return repository, base, candidate


def _assert_consistent_at(repository: Path, commit: str) -> None:
    assert _git(repository, "rev-parse", "refs/heads/main") == commit
    assert _git(repository, "status", "--porcelain", "--untracked-files=no") == ""
    assert not (repository / ".git" / FAST_FORWARD_MARKER).exists()
    assert not (repository / ".git" / "index.lock").exists()


def _manager(tmp_path: Path) -> GitWorktreeManager:
    return GitWorktreeManager(tmp_path / "worktrees")


def _fake_git(tmp_path: Path, seconds: float) -> Path:
    """A Git that sleeps before every branch move or tree sync, then runs real Git."""
    script = tmp_path / "fake_git.py"
    script.write_text(
        "import subprocess, sys, time\n"
        "arguments = sys.argv[1:]\n"
        "if 'update-ref' in arguments or 'read-tree' in arguments:\n"
        f"    time.sleep({seconds})\n"
        "sys.exit(subprocess.call(['git', *arguments]))\n",
        encoding="utf-8",
    )
    return script


def test_fast_forward_moves_the_ref_by_compare_and_set_and_syncs_the_tree(tmp_path):
    repository, base, candidate = _repository(tmp_path)

    merged, head = _manager(tmp_path).fast_forward_default_branch(
        repository_path=str(repository), default_branch="main",
        expected_base_commit=base, candidate_commit=candidate)

    assert (merged, head) == (True, candidate)
    _assert_consistent_at(repository, candidate)
    assert (repository / "src" / "feature.txt").read_text(encoding="utf-8") == "candidate change\n"
    assert not (repository / "gone.txt").exists()
    # The move is recorded in the reflog as HoH's, not as an anonymous merge.
    assert "hoh: fast-forward to candidate" in _git(repository, "reflog", "-1", "refs/heads/main")


def test_a_moved_branch_is_reported_and_left_untouched(tmp_path):
    repository, base, candidate = _repository(tmp_path)
    (repository / "other.txt").write_text("landed meanwhile\n", encoding="utf-8")
    _git(repository, "add", "other.txt")
    _git(repository, "commit", "-m", "outside work")
    moved = _git(repository, "rev-parse", "HEAD")

    merged, head = _manager(tmp_path).fast_forward_default_branch(
        repository_path=str(repository), default_branch="main",
        expected_base_commit=base, candidate_commit=candidate)

    assert (merged, head) == (False, moved)
    _assert_consistent_at(repository, moved)


def test_a_git_slower_than_the_command_budget_still_finishes_the_move(tmp_path, monkeypatch):
    repository, base, candidate = _repository(tmp_path)
    monkeypatch.setattr(git_worktrees, "_GIT_PROGRAM", (sys.executable, str(_fake_git(tmp_path, 3))))
    manager = _manager(tmp_path)
    real_git = manager._git
    mutations = []

    def mutation_budget(path, *arguments, **kwargs):
        if arguments[0] not in {"update-ref", "read-tree"}:
            return real_git(path, *arguments, **kwargs)
        mutations.append(arguments[0])
        # Only the delayed mutation faces the artificial budget; metadata keeps its own.
        with monkeypatch.context() as mutation:
            mutation.setattr(git_worktrees, "_GIT_COMMAND_TIMEOUT_SECONDS", 1)
            return real_git(path, *arguments, **kwargs)

    monkeypatch.setattr(manager, "_git", mutation_budget)

    merged, head = manager.fast_forward_default_branch(
        repository_path=str(repository), default_branch="main",
        expected_base_commit=base, candidate_commit=candidate)

    assert (merged, head) == (True, candidate)
    assert mutations == ["update-ref", "read-tree"]
    _assert_consistent_at(repository, candidate)
    assert (repository / "src" / "feature.txt").read_text(encoding="utf-8") == "candidate change\n"
    assert not (repository / "gone.txt").exists()


def _interrupt_after_the_ref_moved(repository: Path, base: str, candidate: str) -> None:
    """What a worker killed between update-ref and the end of read-tree leaves."""
    (repository / ".git" / FAST_FORWARD_MARKER).write_text(json.dumps(
        {"ref": "refs/heads/main", "old": base, "new": candidate, "checked_out": True}),
        encoding="utf-8")
    _git(repository, "update-ref", "refs/heads/main", candidate, base)
    # read-tree had written one file of the new tree before it died, holding the lock.
    (repository / "src").mkdir()
    (repository / "src" / "feature.txt").write_text("candidate change\n", encoding="utf-8")
    lock = repository / ".git" / "index.lock"
    lock.write_bytes(b"")
    old = time.time() - 30
    os.utime(lock, (old, old))


def test_recovery_finishes_a_move_whose_ref_landed_before_the_worker_died(tmp_path):
    repository, base, candidate = _repository(tmp_path)
    _interrupt_after_the_ref_moved(repository, base, candidate)
    (repository / "notes.txt").write_text("operator scratch, untracked\n", encoding="utf-8")

    result = _manager(tmp_path).recover_interrupted_fast_forward(str(repository))

    assert result["outcome"] == "completed"
    _assert_consistent_at(repository, candidate)
    assert not (repository / "gone.txt").exists()
    assert (repository / "README.md").read_text(encoding="utf-8") == "# project, edited\n"
    # Work the move does not own is kept.
    assert (repository / "notes.txt").exists()


def test_the_next_fast_forward_finishes_an_interrupted_move_first(tmp_path):
    repository, base, candidate = _repository(tmp_path)
    _interrupt_after_the_ref_moved(repository, base, candidate)

    merged, head = _manager(tmp_path).fast_forward_default_branch(
        repository_path=str(repository), default_branch="main",
        expected_base_commit=base, candidate_commit=candidate)

    # Not "must be clean": the half-done move is completed, then seen as drift.
    assert (merged, head) == (False, candidate)
    _assert_consistent_at(repository, candidate)


def test_recovery_drops_the_marker_when_the_ref_never_moved(tmp_path):
    repository, base, candidate = _repository(tmp_path)
    (repository / ".git" / FAST_FORWARD_MARKER).write_text(json.dumps(
        {"ref": "refs/heads/main", "old": base, "new": candidate, "checked_out": True}),
        encoding="utf-8")

    result = _manager(tmp_path).recover_interrupted_fast_forward(str(repository))

    assert result["outcome"] == "not_started"
    _assert_consistent_at(repository, base)
    assert (repository / "gone.txt").exists()


def test_recovery_leaves_a_branch_someone_else_moved_alone(tmp_path):
    repository, base, candidate = _repository(tmp_path)
    (repository / ".git" / FAST_FORWARD_MARKER).write_text(json.dumps(
        {"ref": "refs/heads/main", "old": base, "new": candidate, "checked_out": True}),
        encoding="utf-8")
    (repository / "other.txt").write_text("outside\n", encoding="utf-8")
    _git(repository, "add", "other.txt")
    _git(repository, "commit", "-m", "outside")

    result = _manager(tmp_path).recover_interrupted_fast_forward(str(repository))

    assert result["outcome"] == "superseded"
    assert not (repository / ".git" / FAST_FORWARD_MARKER).exists()
    assert _git(repository, "status", "--porcelain") == ""


def test_an_unreadable_marker_is_dropped(tmp_path):
    repository, base, _candidate = _repository(tmp_path)
    (repository / ".git" / FAST_FORWARD_MARKER).write_text("{not json", encoding="utf-8")

    assert _manager(tmp_path).recover_interrupted_fast_forward(str(repository)) == {
        "outcome": "unreadable_marker_dropped"}
    _assert_consistent_at(repository, base)


def test_no_marker_means_nothing_to_recover(tmp_path):
    repository, _base, _candidate = _repository(tmp_path)
    assert _manager(tmp_path).recover_interrupted_fast_forward(str(repository)) is None


def test_a_fresh_index_lock_refuses_before_the_ref_moves(tmp_path):
    repository, base, candidate = _repository(tmp_path)
    (repository / ".git" / "index.lock").write_bytes(b"")

    with pytest.raises(ValidationError) as refused:
        _manager(tmp_path).fast_forward_default_branch(
            repository_path=str(repository), default_branch="main",
            expected_base_commit=base, candidate_commit=candidate)

    assert refused.value.details["reason"] == "index_lock_busy"
    assert _git(repository, "rev-parse", "refs/heads/main") == base
    # Another process's lock is never removed.
    assert (repository / ".git" / "index.lock").exists()
    assert not (repository / ".git" / FAST_FORWARD_MARKER).exists()


def test_a_stale_index_lock_no_git_can_own_is_cleared(tmp_path):
    repository, base, candidate = _repository(tmp_path)
    lock = repository / ".git" / "index.lock"
    lock.write_bytes(b"")
    hours_ago = time.time() - 7200
    os.utime(lock, (hours_ago, hours_ago))

    merged, _head = _manager(tmp_path).fast_forward_default_branch(
        repository_path=str(repository), default_branch="main",
        expected_base_commit=base, candidate_commit=candidate)

    assert merged
    _assert_consistent_at(repository, candidate)


def test_cas_fast_forward_also_survives_an_interrupted_sync(tmp_path):
    repository, base, candidate = _repository(tmp_path)
    _interrupt_after_the_ref_moved(repository, base, candidate)

    merged, head = _manager(tmp_path).cas_fast_forward(
        repository_path=str(repository), default_branch="main",
        expected_old_commit=base, new_commit=candidate)

    assert (merged, head) == (False, candidate)
    _assert_consistent_at(repository, candidate)


def test_hold_defers_cancel_and_timeout_until_the_phase_ends(tmp_path):
    started = time.monotonic()
    result = run_managed([sys.executable, "-c", "import time; time.sleep(1.5)"],
                         cwd=tmp_path, timeout=0.3, cancel=lambda: True, hold=lambda: True)
    assert result.returncode == 0
    assert not result.cancelled and not result.timed_out
    assert time.monotonic() - started >= 1.4


def test_cancel_applies_again_once_the_phase_ends(tmp_path):
    phase_ends = time.monotonic() + 0.5
    result = run_managed([sys.executable, "-c", "import time; time.sleep(30)"],
                         cwd=tmp_path, timeout=60, cancel=lambda: True,
                         hold=lambda: time.monotonic() < phase_ends)
    assert result.cancelled


def test_a_hung_phase_is_still_stopped_after_the_hold_limit(tmp_path):
    started = time.monotonic()
    result = run_managed([sys.executable, "-c", "import time; time.sleep(30)"],
                         cwd=tmp_path, timeout=0.3, hold=lambda: True, hold_limit=0.5)
    assert result.timed_out
    assert time.monotonic() - started < 20


def test_a_supervisor_stop_during_a_slow_branch_move_leaves_ref_and_tree_consistent(
    service, actors, seeded, tmp_path, monkeypatch
):
    """The R6 acceptance check: fake Git sleeps 20 s while ``stopping`` is set."""
    from helpers.integration import _candidate_ready_for_integration, _claimed_for_integration
    from hoh_control_plane.supervisor import Supervisor
    from hoh_control_plane.uncancellable import is_uncancellable, marker_path

    repository, ticket, candidate, verified = _candidate_ready_for_integration(
        service, actors, seeded, tmp_path)
    base = _git(repository, "rev-parse", "main")
    integration = _claimed_for_integration(service, actors, ticket, verified)
    queued = service.queue_merge_candidate(
        actors["integrator"], ticket["ticket_id"], integration["lease"]["lease_id"],
        integration["ticket"]["version"],
        context_snapshot_id=service.get_ticket_context(
            actors["integrator"], ticket["ticket_id"])["context_snapshot_id"])
    merge_job_id = queued["merge_job"]["merge_job_id"]
    fake_git = _fake_git(tmp_path, 20)
    wrapper = tmp_path / "slow_git_worker.py"
    wrapper.write_text(
        "import runpy, sys\n"
        "from hoh_control_plane import git_worktrees\n"
        f"git_worktrees._GIT_PROGRAM = (sys.executable, {str(fake_git)!r})\n"
        "sys.argv = ['managed_worker', *sys.argv[1:]]\n"
        "runpy.run_module('hoh_control_plane.managed_worker', run_name='__main__')\n",
        encoding="utf-8",
    )

    with Supervisor(service, max_workers=1, worker_timeout=600) as supervisor:
        real_command = supervisor._worker_command
        monkeypatch.setattr(supervisor, "_worker_command", lambda *a: [
            sys.executable, str(wrapper), *real_command(*a)[3:]])
        supervisor.tick()
        assert ("merge", merge_job_id) in supervisor.active
        deadline = time.monotonic() + 300
        while not is_uncancellable(service.store.artifact_root, "merge", merge_job_id):
            assert time.monotonic() < deadline, "the worker never reached the branch move"
            assert not supervisor.active[("merge", merge_job_id)].done()
            time.sleep(0.1)
        # Shutdown arrives mid-move; the lease lapsing too must not cut it short.
        supervisor.stopping.set()
        with service.store.connection(immediate=True) as connection:
            connection.execute("UPDATE leases SET expires_at = '2000-01-01T00:00:00+00:00' "
                               "WHERE lease_id = ?", (integration["lease"]["lease_id"],))
        supervisor.active[("merge", merge_job_id)].result(timeout=600)

    commit = candidate["candidate"]["commit_sha"]
    assert commit != base
    _assert_consistent_at(repository, commit)
    assert (repository / "src" / "checkout" / "retry.txt").read_text(encoding="utf-8") == "safe retry\n"
    assert not marker_path(service.store.artifact_root, "merge", merge_job_id).exists()
    with service.store.connection() as connection:
        job = connection.execute("SELECT status, merge_status FROM merge_jobs WHERE merge_job_id = ?",
                                 (merge_job_id,)).fetchone()
    # The worker was stopped after the move; recovery records the landing it made.
    assert (job["status"], job["merge_status"]) == ("COMPLETED", "merged")
