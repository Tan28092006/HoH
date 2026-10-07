"""The shipped Git budget is exercised, not only the suite's raised one (review E5).

tests/conftest.py raises the 15 s Git budget to 600 s so a loaded machine does
not fail tests for being slow. That left the production value untested. These
run under the shipped value (marker ``production_git_timeout``).
"""
from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest
from helpers.integration import _git, _repository

from hoh_control_plane import git_worktrees
from hoh_control_plane.domain import ValidationError
from hoh_control_plane.git_worktrees import GitWorktreeManager

pytestmark = pytest.mark.production_git_timeout


def test_the_shipped_budget_is_fifteen_seconds_here() -> None:
    assert git_worktrees._GIT_COMMAND_TIMEOUT_SECONDS == 15


def test_real_git_commands_run_and_finish_under_the_shipped_budget(tmp_path) -> None:
    repository = _repository(tmp_path)
    seen: list[float] = []
    real_run = git_worktrees.run_managed

    def spy(command, **kwargs):
        seen.append(kwargs["timeout"])
        return real_run(command, **kwargs)

    manager = GitWorktreeManager(tmp_path / "worktrees")
    with patch.object(git_worktrees, "run_managed", spy):
        head = manager._resolve_commit(repository, "HEAD")
        status = manager._git(repository, "status", "--porcelain").stdout

    assert head == _git(repository, "rev-parse", "HEAD")
    assert status == ""
    assert seen and set(seen) == {15}


def test_a_git_command_over_the_budget_becomes_a_domain_error(tmp_path) -> None:
    manager = GitWorktreeManager(tmp_path / "worktrees")
    with patch.object(git_worktrees, "run_managed",
                      side_effect=subprocess.TimeoutExpired(["git", "status"], 15)):
        with pytest.raises(ValidationError, match="timed out"):
            manager._git(tmp_path, "status", "--porcelain")
