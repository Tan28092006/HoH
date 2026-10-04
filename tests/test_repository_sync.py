from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from barrier_helpers import keep_open_tickets

from hoh_control_plane.domain import (
    AuthorizationError,
    ConflictError,
    ContextStaleError,
    ValidationError,
)
from hoh_control_plane.mcp_server import create_mcp_server


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


def test_sync_repository_fetches_and_classifies_remote_changes_without_pulling(
    service, actors, seeded, tmp_path: Path
) -> None:
    repository, remote = _tracked_repository(tmp_path)
    service.configure_repository(actors["planner"], "project-1", str(repository))
    assert service.sync_repository(actors["planner"], "project-1")["relation"] == "up_to_date"
    packet = service.get_ticket_context(actors["developer"], seeded["ticket"]["ticket_id"])
    unchanged_head = _git(repository, "rev-parse", "HEAD")
    service.sync_repository(actors["planner"], "project-1")
    with service.store.connection(immediate=True) as connection:
        service.require_fresh_context(
            connection, seeded["ticket"]["ticket_id"], packet["context_snapshot_id"]
        )

    friend = tmp_path / "friend"
    subprocess.run(["git", "clone", str(remote), str(friend)], check=True, capture_output=True)
    _git(friend, "config", "user.email", "friend@example.invalid")
    _git(friend, "config", "user.name", "Friend")
    _git(friend, "checkout", "main")
    (friend / "docs").mkdir()
    (friend / "docs" / "requirements.md").write_text("New production requirement\n")
    (friend / "src").mkdir()
    (friend / "src" / "api.py").write_text("STATUS = 'deployed'\n")
    _git(friend, "add", "docs/requirements.md", "src/api.py")
    _git(friend, "commit", "-m", "Add production requirement and API status")
    _git(friend, "push", "origin", "main")
    _git(friend, "checkout", "-b", "shared-feature")
    (friend / "shared.txt").write_text("parallel branch\n")
    _git(friend, "add", "shared.txt")
    _git(friend, "commit", "-m", "Start shared feature")
    _git(friend, "push", "origin", "shared-feature")

    manifest = service.sync_repository(actors["planner"], "project-1")

    assert manifest["relation"] == "behind"
    assert manifest["review_required"] is True
    assert manifest["behind"] == 1
    assert {row["path"] for row in manifest["incoming_changes"]} == {
        "docs/requirements.md", "src/api.py"
    }
    # Document paths include those changed on any fetched branch, not only main.
    assert manifest["document_paths"] == ["docs/requirements.md", "shared.txt"]
    updates = {row["branch"]: row for row in manifest["branch_updates"]}
    assert updates["origin/main"]["change"] == "updated"
    assert updates["origin/shared-feature"]["change"] == "new"
    assert _git(repository, "rev-parse", "HEAD") == unchanged_head
    # The observed commit opens a barrier, which refuses a claim before its
    # snapshot is even compared; the snapshot is stale on its own as well.
    with pytest.raises(ConflictError, match="Planner must reconcile"):
        service.claim_ticket(
            actors["developer"], seeded["ticket"]["ticket_id"],
            seeded["ticket"]["version"], 900,
            context_snapshot_id=packet["context_snapshot_id"],
        )
    with service.store.connection(immediate=True) as connection:
        with pytest.raises(ContextStaleError) as stale:
            service.require_fresh_context(
                connection, seeded["ticket"]["ticket_id"], packet["context_snapshot_id"]
            )
    assert "repository" in stale.value.details["stale"]

    _git(friend, "push", "origin", "--delete", "shared-feature")
    deleted = service.sync_repository(actors["planner"], "project-1")
    deletion = {row["branch"]: row for row in deleted["branch_updates"]}
    assert deletion["origin/shared-feature"]["change"] == "deleted"


def test_a_remote_update_to_the_ticket_branch_stales_only_its_snapshot(
    service, actors, seeded, tmp_path: Path
) -> None:
    repository, remote = _tracked_repository(tmp_path)
    service.configure_repository(actors["planner"], "project-1", str(repository))
    service.sync_repository(actors["planner"], "project-1")
    # The first observation is an iteration boundary Planner resolves first.
    keep_open_tickets(service, actors["planner"])
    ticket = seeded["ticket"]
    claimed = service.claim_ticket(
        actors["developer"], ticket["ticket_id"], ticket["version"], 900
    )
    prepared = service.prepare_worktree(
        actors["developer"], ticket["ticket_id"], claimed["lease"]["lease_id"],
        claimed["ticket"]["version"],
    )
    packet = service.get_ticket_context(actors["developer"], ticket["ticket_id"])

    friend = tmp_path / "ticket-branch-writer"
    subprocess.run(["git", "clone", str(remote), str(friend)], check=True, capture_output=True)
    _git(friend, "config", "user.email", "friend@example.invalid")
    _git(friend, "config", "user.name", "Friend")
    _git(friend, "checkout", "main")
    _git(friend, "checkout", "-b", prepared["worktree"]["branch_ref"])
    (friend / "shared-ticket.txt").write_text("new shared work\n")
    _git(friend, "add", "shared-ticket.txt")
    _git(friend, "commit", "-m", "Advance the shared ticket branch")
    _git(friend, "push", "origin", prepared["worktree"]["branch_ref"])

    manifest = service.sync_repository(actors["planner"], "project-1")
    assert any(
        row["branch"] == "origin/" + prepared["worktree"]["branch_ref"]
        for row in manifest["branch_updates"]
    )
    with service.store.connection(immediate=True) as connection:
        with pytest.raises(ContextStaleError) as stale:
            service.require_fresh_context(
                connection, ticket["ticket_id"], packet["context_snapshot_id"]
            )
    # The push is also an iteration boundary, so the barrier section is stale
    # too; nothing about the ticket, feature or candidate is.
    assert sorted(stale.value.details["stale"]) == ["planning_barrier", "repository"]


def test_only_planner_can_sync_repository(service, actors, seeded, tmp_path: Path) -> None:
    repository, _remote = _tracked_repository(tmp_path)
    service.configure_repository(actors["planner"], "project-1", str(repository))

    with pytest.raises(AuthorizationError):
        service.sync_repository(actors["developer"], "project-1")


def test_sync_repository_reports_no_upstream_without_mutating_a_local_repository(
    service, actors, seeded, tmp_path: Path
) -> None:
    repository = tmp_path / "local-only"
    repository.mkdir()
    _git(repository, "init", "--initial-branch=main")
    _git(repository, "config", "user.email", "hoh-tests@example.invalid")
    _git(repository, "config", "user.name", "HoH Tests")
    (repository / "README.md").write_text("local\n")
    _git(repository, "add", "README.md")
    _git(repository, "commit", "-m", "Local project")
    service.configure_repository(actors["planner"], "project-1", str(repository))

    manifest = service.sync_repository(actors["planner"], "project-1")

    assert manifest["relation"] == "no_upstream"
    assert manifest["upstream_commit"] is None
    assert manifest["branch_updates"] == []


def test_sync_repository_fetches_remote_refs_even_without_a_tracking_branch(
    service, actors, seeded, tmp_path: Path
) -> None:
    repository, _remote = _tracked_repository(tmp_path)
    _git(repository, "config", "--unset", "branch.main.remote")
    _git(repository, "config", "--unset", "branch.main.merge")
    # Keep a remote configured while removing only branch tracking.
    _git(repository, "config", "branch.main.remote", "origin")
    service.configure_repository(actors["planner"], "project-1", str(repository))

    manifest = service.sync_repository(actors["planner"], "project-1")

    assert manifest["relation"] == "no_upstream"
    assert manifest["branch_updates"][0]["branch"] == "origin/main"


def test_sync_repository_fails_closed_when_the_configured_remote_is_invalid(
    service, actors, seeded, tmp_path: Path
) -> None:
    repository, _remote = _tracked_repository(tmp_path)
    service.configure_repository(actors["planner"], "project-1", str(repository))
    _git(repository, "remote", "set-url", "origin", str(tmp_path / "missing.git"))

    with pytest.raises(ValidationError) as failed:
        service.sync_repository(actors["planner"], "project-1")
    assert failed.value.details["reason"] == "upstream_fetch_failed"


def test_sync_repository_classifies_ahead_then_diverged_history(
    service, actors, seeded, tmp_path: Path
) -> None:
    repository, remote = _tracked_repository(tmp_path)
    service.configure_repository(actors["planner"], "project-1", str(repository))
    service.sync_repository(actors["planner"], "project-1")
    (repository / "local.txt").write_text("local\n")
    _git(repository, "add", "local.txt")
    _git(repository, "commit", "-m", "Local-only work")

    ahead = service.sync_repository(actors["planner"], "project-1")
    assert ahead["relation"] == "ahead"
    assert ahead["ahead"] == 1 and ahead["behind"] == 0
    assert ahead["local_changes"] == [{"status": "A", "path": "local.txt"}]

    friend = tmp_path / "diverging-friend"
    subprocess.run(["git", "clone", str(remote), str(friend)], check=True, capture_output=True)
    _git(friend, "config", "user.email", "friend@example.invalid")
    _git(friend, "config", "user.name", "Friend")
    _git(friend, "checkout", "main")
    (friend / "remote.txt").write_text("remote\n")
    _git(friend, "add", "remote.txt")
    _git(friend, "commit", "-m", "Remote-only work")
    _git(friend, "push", "origin", "main")

    diverged = service.sync_repository(actors["planner"], "project-1")
    assert diverged["relation"] == "diverged"
    assert diverged["ahead"] == diverged["behind"] == 1
    assert diverged["incoming_changes"] == [{"status": "A", "path": "remote.txt"}]


@pytest.mark.anyio
async def test_planner_sync_repository_is_callable_through_mcp(
    service, actors, seeded, tmp_path: Path
) -> None:
    repository, _remote = _tracked_repository(tmp_path)
    service.configure_repository(actors["planner"], "project-1", str(repository))

    _content, manifest = await create_mcp_server(service, actors["planner"]).call_tool(
        "planner.sync_repository", {"project_id": "project-1"}
    )

    assert manifest["relation"] == "up_to_date"


def test_a_merge_landing_during_the_fetch_is_not_overwritten_or_reconciled_twice(
    service, actors, seeded, tmp_path: Path, monkeypatch
) -> None:
    repository, _remote = _tracked_repository(tmp_path)
    service.configure_repository(actors["planner"], "project-1", str(repository))
    service.sync_repository(actors["planner"], "project-1")
    keep_open_tickets(service, actors["planner"])

    def reconciliations() -> list[dict]:
        with service.store.connection() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT reconciliation_id, status FROM repository_reconciliations "
                "WHERE project_id = 'project-1'"
            ).fetchall()]

    before = reconciliations()
    real_sync_upstream = service.git_worktrees.sync_upstream
    calls: list[str] = []
    merged: dict[str, str] = {}

    def sync_upstream_with_concurrent_merge(repository_path, default_branch):
        manifest = real_sync_upstream(repository_path, default_branch)
        calls.append(manifest["local_commit"])
        if len(calls) == 1:
            # A queued merge completes after the fetch but before the sync
            # commits: Git's branch and the observed mainline both advance.
            (repository / "merged.txt").write_text("merged\n", encoding="utf-8")
            _git(repository, "add", "merged.txt")
            _git(repository, "commit", "-m", "Merged candidate")
            merged["commit"] = _git(repository, "rev-parse", "HEAD")
            with service.store.connection(immediate=True) as connection:
                connection.execute(
                    "UPDATE project_repositories SET observed_mainline_commit = ? "
                    "WHERE project_id = 'project-1'",
                    (merged["commit"],),
                )
        return manifest

    monkeypatch.setattr(service.git_worktrees, "sync_upstream", sync_upstream_with_concurrent_merge)
    manifest = service.sync_repository(actors["planner"], "project-1")

    assert len(calls) == 2
    assert calls[1] == merged["commit"]
    assert manifest["local_commit"] == merged["commit"]
    assert manifest["changed"] is False
    assert manifest["reconciliation_id"] is None
    assert reconciliations() == before
    with service.store.connection() as connection:
        observed = connection.execute(
            "SELECT observed_mainline_commit FROM project_repositories WHERE project_id = 'project-1'"
        ).fetchone()[0]
    assert observed == merged["commit"]
