"""A Developer candidate committed in a prepared worktree, not yet submitted."""
from __future__ import annotations

import subprocess
from pathlib import Path


def _git(path: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(path), *arguments], check=True, capture_output=True, text=True
    )
    return completed.stdout.strip()


def _prepared_candidate(service, actors, seeded, tmp_path: Path):
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "--initial-branch=main")
    _git(repository, "config", "user.email", "hoh-tests@example.invalid")
    _git(repository, "config", "user.name", "HoH Tests")
    (repository / "README.md").write_text("# project\n", encoding="utf-8")
    _git(repository, "add", "README.md")
    _git(repository, "commit", "-m", "Initial commit")
    service.configure_repository(actors["planner"], "project-1", str(repository))
    ticket = seeded["ticket"]
    claim = service.claim_ticket(actors["developer"], ticket["ticket_id"], ticket["version"], 900)
    prepared = service.prepare_worktree(
        actors["developer"], ticket["ticket_id"], claim["lease"]["lease_id"], claim["ticket"]["version"]
    )["worktree"]
    worktree = Path(prepared["worktree_path"])
    change = worktree / "src" / "checkout" / "retry.txt"
    change.parent.mkdir(parents=True)
    change.write_text("safe retry\n", encoding="utf-8")
    _git(worktree, "add", "src/checkout/retry.txt")
    _git(worktree, "commit", "-m", "Implement retry")
    return ticket, claim, prepared, _git(worktree, "rev-parse", "HEAD")
