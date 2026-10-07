"""A repository cloned from a bare remote whose refspec tracks only main."""
from __future__ import annotations

import subprocess
from pathlib import Path


def _git(path: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *arguments],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _tracked_repository(tmp_path: Path) -> tuple[Path, Path]:
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    repository = tmp_path / "repository"
    subprocess.run(["git", "clone", str(remote), str(repository)], check=True, capture_output=True)
    _git(repository, "config", "user.email", "hoh-tests@example.invalid")
    _git(repository, "config", "user.name", "HoH Tests")
    (repository / "README.md").write_text("# Project\n", encoding="utf-8")
    _git(repository, "add", "README.md")
    _git(repository, "commit", "-m", "Initial commit")
    _git(repository, "branch", "-M", "main")
    _git(repository, "push", "-u", "origin", "main")
    # Simulate a client whose ordinary refspec only tracks main. HoH must still
    # fetch every branch because shared work can live outside the default branch.
    _git(repository, "config", "--replace-all", "remote.origin.fetch", "+refs/heads/main:refs/remotes/origin/main")
    return repository, remote
