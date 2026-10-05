"""Agents read 12-character commits and may write abbreviated ones.

A full SHA costs about ten tokens every time a packet repeats it, and a packet
repeats the candidate and base commit several times. What an agent reads names
commits as 12 hex characters; the store keeps the full value, and a tool that
takes a commit resolves a prefix in the project repository before it compares
anything.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

import hoh_control_plane.service.context as context_module
from hoh_control_plane.domain import ValidationError
from hoh_control_plane.mcp_server import create_mcp_server
from hoh_control_plane.service._common import agent_facing, delivered_json

FULL_SHA = re.compile(r"[0-9a-f]{40}")


def _git(path: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *arguments], check=True, capture_output=True, text=True
    ).stdout.strip()


def _developer_commit(service, actors, seeded, tmp_path: Path) -> dict:
    """A claimed ticket with a prepared worktree and one commit ready to submit."""
    repository = tmp_path / "short-sha-repository"
    repository.mkdir()
    _git(repository, "init", "--initial-branch=main")
    _git(repository, "config", "user.email", "hoh-tests@example.invalid")
    _git(repository, "config", "user.name", "HoH Tests")
    (repository / "README.md").write_text("# Checkout\n", encoding="utf-8")
    _git(repository, "add", "README.md")
    _git(repository, "commit", "-m", "Initial")
    service.configure_repository(actors["planner"], "project-1", str(repository))
    ticket = seeded["ticket"]
    developer = actors["developer"]
    claim = service.claim_ticket(developer, ticket["ticket_id"], ticket["version"], 900)
    prepared = service.prepare_worktree(
        developer, ticket["ticket_id"], claim["lease"]["lease_id"], claim["ticket"]["version"]
    )
    worktree = Path(prepared["worktree"]["worktree_path"])
    (worktree / "src" / "checkout").mkdir(parents=True)
    (worktree / "src" / "checkout" / "retry.txt").write_text("retry\n", encoding="utf-8")
    _git(worktree, "add", ".")
    _git(worktree, "commit", "-m", "Retry")
    return {
        "ticket_id": ticket["ticket_id"],
        "lease_id": claim["lease"]["lease_id"],
        "commit": _git(worktree, "rev-parse", "HEAD"),
        "base": prepared["worktree"]["base_commit"],
        "branch": prepared["worktree"]["branch_ref"],
        "repository": repository,
    }


def _submit(service, actors, work: dict, commit: str, base: str) -> dict:
    developer = actors["developer"]
    packet = service.get_ticket_context(developer, work["ticket_id"])
    return service.submit_candidate(
        developer, work["ticket_id"], work["lease_id"], packet["ticket"]["version"],
        commit, base, work["branch"], ["Ready."],
        context_snapshot_id=packet["context_snapshot_id"],
    )


def test_the_helper_shortens_shas_and_digests_but_not_references() -> None:
    sha, digest = "a1" * 20, "b2" * 32
    shown = agent_facing({
        "commit_sha": sha,
        "environment_digest": digest,
        "content": f"Base drifted from {sha} to {digest[:40]}.",
        "branch_ref": f"refs/hoh/{sha}",
        "nested": [sha, {"x": sha}],
        "count": 3,
    })

    assert shown["commit_sha"] == sha[:12]
    assert shown["environment_digest"] == digest[:12]
    assert shown["content"] == f"Base drifted from {sha[:12]} to {digest[:12]}."
    # A reference an agent may pass back is left exactly as stored.
    assert shown["branch_ref"] == f"refs/hoh/{sha}"
    assert shown["nested"] == [sha[:12], {"x": sha[:12]}]
    assert agent_facing(shown) == shown


@pytest.mark.anyio
async def test_a_qa_ready_packet_shows_no_full_sha(
    service, actors, seeded, tmp_path, monkeypatch
) -> None:
    work = _developer_commit(service, actors, seeded, tmp_path)
    submitted = _submit(service, actors, work, work["commit"], work["base"])
    assert submitted["ticket"]["status"] == "QA_READY"

    packet = service.get_ticket_context(actors["qa"], work["ticket_id"])

    assert not FULL_SHA.search(delivered_json(packet))
    assert packet["candidate"]["commit_sha"] == work["commit"][:12]
    assert packet["candidate"]["base_commit"] == work["base"][:12]
    _content, structured = await create_mcp_server(service, actors["qa"]).call_tool(
        "ticket.get_context", {"ticket_ref": work["ticket_id"]}
    )
    assert not FULL_SHA.search(delivered_json(structured))

    # The budget measures what is delivered, so the estimate drops with it.
    shortened = packet["estimated_tokens"]
    monkeypatch.setattr(context_module, "agent_facing", lambda payload: payload)
    full = service.get_ticket_context(actors["qa"], work["ticket_id"])["estimated_tokens"]
    assert shortened < full
    print(f"QA_READY packet estimated tokens: {full} with full SHAs, {shortened} shortened")


def test_a_short_sha_is_submitted_and_stored_in_full(service, actors, seeded, tmp_path) -> None:
    work = _developer_commit(service, actors, seeded, tmp_path)

    submitted = _submit(service, actors, work, work["commit"][:12], work["base"][:7])

    assert submitted["candidate"]["commit_sha"] == work["commit"]
    assert submitted["candidate"]["base_commit"] == work["base"]
    with service.store.connection() as connection:
        row = connection.execute(
            "SELECT commit_sha, base_commit FROM candidates WHERE ticket_id = ?",
            (work["ticket_id"],),
        ).fetchone()
        ticket = service._ticket(connection, work["ticket_id"])
    assert (row["commit_sha"], row["base_commit"]) == (work["commit"], work["base"])
    assert ticket["candidate_commit"] == work["commit"]


def test_an_unknown_short_or_unresolvable_prefix_is_refused(
    service, actors, seeded, tmp_path
) -> None:
    work = _developer_commit(service, actors, seeded, tmp_path)
    unknown = "0" * 12 if not work["commit"].startswith("0" * 12) else "f" * 12

    with pytest.raises(ValidationError, match="names no commit") as refused:
        _submit(service, actors, work, unknown, work["base"])
    assert refused.value.details["reason"] == "commit_unknown"
    with pytest.raises(ValidationError, match="at least 7") as refused:
        _submit(service, actors, work, work["commit"][:6], work["base"])
    assert refused.value.details["reason"] == "commit_invalid"
    # A branch spelled in hex resolves in Git, but it is not a commit prefix.
    _git(work["repository"], "branch", "abcdef1", work["base"])
    with pytest.raises(ValidationError, match="names no commit"):
        _submit(service, actors, work, work["commit"], "abcdef1")


def test_an_ambiguous_prefix_is_refused(service, actors, seeded, tmp_path, monkeypatch) -> None:
    work = _developer_commit(service, actors, seeded, tmp_path)
    git = service.git_worktrees._git

    def two_commits_share_the_prefix(repository, *arguments, check=True):
        if arguments and arguments[0] == "rev-parse" and "--quiet" in arguments:
            return subprocess.CompletedProcess(arguments, 1, "", "")
        if arguments and str(arguments[-1]).startswith("--disambiguate="):
            return subprocess.CompletedProcess(
                arguments, 0, f"{work['commit']}\n{work['commit'][:12]}{'0' * 28}\n", ""
            )
        return git(repository, *arguments, check=check)

    monkeypatch.setattr(service.git_worktrees, "_git", two_commits_share_the_prefix)

    with pytest.raises(ValidationError, match="ambiguous") as refused:
        _submit(service, actors, work, work["commit"][:12], work["base"])
    assert refused.value.details["reason"] == "commit_prefix_ambiguous"


def test_an_abbreviated_sha_needs_a_repository(service, actors, seeded) -> None:
    ticket = seeded["ticket"]
    developer = actors["developer"]
    claim = service.claim_ticket(developer, ticket["ticket_id"], ticket["version"], 900)

    with pytest.raises(ValidationError, match="pass the full SHA") as refused:
        service.submit_candidate(
            developer, ticket["ticket_id"], claim["lease"]["lease_id"],
            claim["ticket"]["version"], "a" * 12, "b" * 40, "branch", ["Ready."],
            context_snapshot_id=claim["current_context_snapshot_id"],
        )
    assert refused.value.details["reason"] == "commit_unresolvable"
