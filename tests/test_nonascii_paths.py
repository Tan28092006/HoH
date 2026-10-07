"""Paths are read from Git with -z, so unusual names are never C-quoted (wave 8E).

Without -z Git renders ``src/tiếng/việt.txt`` as ``"src/ti\\341\\272\\277ng/..."``:
the quoted text matches no write scope, so an in-scope change is refused (or a
reported path is wrong). A recovery patch is kept as Git's own bytes.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from hoh_control_plane.domain import ValidationError
from hoh_control_plane.git_worktrees import GitWorktreeManager

from helpers.candidate import _prepared_candidate

UNUSUAL = ["src/checkout/tiếng việt.txt", "src/checkout/émoji 🙂.txt", "src/checkout/ñ/日本.md"]


def _git(path: Path, *arguments: str) -> str:
    return subprocess.run(["git", "-C", str(path), *arguments], check=True,
                          capture_output=True, text=True, encoding="utf-8").stdout.strip()


def _repository(tmp_path: Path) -> tuple[Path, str, str]:
    repository = tmp_path / "paths"
    repository.mkdir()
    _git(repository, "init", "--initial-branch=main")
    _git(repository, "config", "user.email", "hoh-tests@example.invalid")
    _git(repository, "config", "user.name", "HoH Tests")
    _git(repository, "config", "core.autocrlf", "false")
    (repository / "README.md").write_text("# project\n", encoding="utf-8")
    (repository / "đã xoá.txt").write_text("deleted later\n", encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "base")
    base = _git(repository, "rev-parse", "HEAD")
    for relative in UNUSUAL:
        target = repository / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("change\n", encoding="utf-8")
    _git(repository, "rm", "-q", "đã xoá.txt")
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "unusual names")
    return repository, base, _git(repository, "rev-parse", "HEAD")


def test_changed_paths_are_the_names_in_the_tree(tmp_path):
    repository, base, head = _repository(tmp_path)

    paths = GitWorktreeManager(tmp_path / "worktrees").changed_paths(
        repository_path=str(repository), base_commit=base, candidate_commit=head)

    assert paths == sorted([*UNUSUAL, "đã xoá.txt"])


def test_observed_commit_changes_name_paths_unquoted(tmp_path):
    repository, base, head = _repository(tmp_path)
    manager = GitWorktreeManager(tmp_path / "worktrees")

    moved = manager.remote_commit_delta(str(repository), base, head)
    single = manager.remote_commit_delta(str(repository), None, head)

    expected = sorted([("A", path) for path in UNUSUAL] + [("D", "đã xoá.txt")])
    for delta in (moved, single):
        assert sorted((row["status"], row["path"]) for row in delta["changes"]) == expected


def test_untracked_paths_survive_quoting_and_renames():
    status = "\0".join([
        "?? src/tiếng việt.txt",
        "R  new name.txt", "old name.txt",
        '?? q"uote.txt',
        " M tracked.txt",
        "",
    ])
    assert GitWorktreeManager._untracked_paths(status) == ('q"uote.txt', "src/tiếng việt.txt")


def test_a_candidate_with_non_ascii_paths_in_scope_is_accepted(service, actors, seeded, tmp_path):
    ticket, claim, prepared, _commit = _prepared_candidate(service, actors, seeded, tmp_path)
    worktree = Path(prepared["worktree_path"])
    unusual = worktree / "src" / "checkout" / "thử lại.txt"
    unusual.write_text("retry\n", encoding="utf-8")
    _git(worktree, "add", "src/checkout/thử lại.txt")
    # One iteration is one commit: fold the name into the prepared one.
    _git(worktree, "commit", "--amend", "--no-edit")

    submitted = service.submit_candidate(
        actors["developer"], ticket["ticket_id"], claim["lease"]["lease_id"],
        claim["ticket"]["version"], _git(worktree, "rev-parse", "HEAD"),
        prepared["base_commit"], prepared["branch_ref"], ["Ready."],
        context_snapshot_id=service.get_ticket_context(
            actors["developer"], ticket["ticket_id"])["context_snapshot_id"])

    assert submitted["ticket"]["status"] == "QA_READY"
    assert "src/checkout/thử lại.txt" in submitted["candidate"]["changed_paths"]


def test_a_non_ascii_path_outside_scope_is_named_as_it_is(service, actors, seeded, tmp_path):
    ticket, claim, prepared, _commit = _prepared_candidate(service, actors, seeded, tmp_path)
    worktree = Path(prepared["worktree_path"])
    outside = worktree / "docs" / "ghi chú.md"
    outside.parent.mkdir()
    outside.write_text("outside\n", encoding="utf-8")
    _git(worktree, "add", "docs/ghi chú.md")
    _git(worktree, "commit", "--amend", "--no-edit")

    with pytest.raises(ValidationError, match=re.escape("docs/ghi chú.md")):
        service.submit_candidate(
            actors["developer"], ticket["ticket_id"], claim["lease"]["lease_id"],
            claim["ticket"]["version"], _git(worktree, "rev-parse", "HEAD"),
            prepared["base_commit"], prepared["branch_ref"], ["Ready."],
            context_snapshot_id=service.get_ticket_context(
                actors["developer"], ticket["ticket_id"])["context_snapshot_id"])


def test_a_recovery_patch_keeps_gits_bytes_and_applies_again(tmp_path):
    repository, _base, _head = _repository(tmp_path)
    legacy = repository / "legacy-latin1.txt"
    legacy.write_bytes("caf\xe9 cr\xe8me\n".encode("latin-1"))
    _git(repository, "add", "legacy-latin1.txt")
    _git(repository, "commit", "-m", "A file that is not UTF-8")
    clean = _git(repository, "rev-parse", "HEAD")
    legacy.write_bytes("caf\xe9 cr\xe8me br\xfbl\xe9e\n".encode("latin-1"))
    expected = subprocess.run(["git", "-C", str(repository), "diff", "HEAD", "--binary"],
                              check=True, capture_output=True).stdout

    artifact = GitWorktreeManager(tmp_path / "worktrees").capture_recovery_patch(
        worktree_path=str(repository), destination=tmp_path / "recovery" / "work.patch")

    payload = (tmp_path / "recovery" / "work.patch").read_bytes()
    assert payload == expected
    assert artifact.byte_size == len(expected)
    # The patch restores the exact bytes on a clean checkout.
    _git(repository, "checkout", "-q", "--", "legacy-latin1.txt")
    assert _git(repository, "rev-parse", "HEAD") == clean
    _git(repository, "apply", str(tmp_path / "recovery" / "work.patch"))
    assert legacy.read_bytes() == "caf\xe9 cr\xe8me br\xfbl\xe9e\n".encode("latin-1")
