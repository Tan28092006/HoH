"""Gaps the audit found whose fix belongs to an open wave 8 branch.

Each test states the correct behavior and is a strict xfail: when the owning
branch lands the fix, the test passes, strict mode turns that into a failure,
and whoever merges removes the marker. Nothing here is fixed on this branch.

The R9 gap (a passed verdict beside a failed finding) was fixed by wave 8A
and is covered by test_verdict_integrity.py.
"""
from __future__ import annotations

import pytest
from helpers.integration import _git, _repository

from hoh_control_plane.git_worktrees import GitWorktreeManager


@pytest.mark.xfail(strict=True, reason="A2, wave 8E: changed_paths must read -z, not C-quoted names")
def test_changed_paths_returns_non_ascii_names_verbatim(tmp_path) -> None:
    repository = _repository(tmp_path)
    base = _git(repository, "rev-parse", "HEAD")
    name = "src/checkout/thành-toán.txt"
    path = repository / name
    path.parent.mkdir(parents=True)
    path.write_text("retry\n", encoding="utf-8")
    _git(repository, "add", name)
    _git(repository, "commit", "-m", "Non-ASCII path")
    head = _git(repository, "rev-parse", "HEAD")

    paths = GitWorktreeManager(tmp_path / "worktrees").changed_paths(
        repository_path=str(repository), base_commit=base, candidate_commit=head)

    assert paths == [name]
