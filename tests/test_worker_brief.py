"""A claim hands the dispatcher a brief a fresh subagent can start from.

Each execution role used to be one long chat that carried every ticket's
transcript into the next. The claim now returns a compact, self-contained brief
so the waiting chat can delegate one ticket to a new agent and keep only the
structured result.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from hoh_control_plane.service._common import _estimate_tokens

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CALLED_TOOL = re.compile(r"^([a-z_]+\.[a-z_]+)\(")


def _git(path: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *arguments], check=True, capture_output=True, text=True
    ).stdout.strip()


def _repository(tmp_path: Path) -> Path:
    repository = tmp_path / "brief-repository"
    repository.mkdir()
    _git(repository, "init", "--initial-branch=main")
    _git(repository, "config", "user.email", "hoh-tests@example.invalid")
    _git(repository, "config", "user.name", "HoH Tests")
    (repository / "README.md").write_text("# Checkout\n", encoding="utf-8")
    _git(repository, "add", "README.md")
    _git(repository, "commit", "-m", "Initial")
    return repository


def _registered_tools() -> set[str]:
    tools = REPOSITORY_ROOT / "src" / "hoh_control_plane" / "mcp" / "tools"
    text = "".join(path.read_text(encoding="utf-8") for path in tools.glob("*.py"))
    return set(re.findall(r'@server\.tool\(name="([^"]+)"', text))


def _assert_resolvable(service, actor, brief: dict) -> None:
    """Every name in the brief points at something that exists right now."""
    ticket_id = brief["ticket_id"]
    again = service.get_ticket_context(
        actor, ticket_id, known_snapshot_id=brief["context_snapshot_id"]
    )
    assert again == {"unchanged": True, "context_snapshot_id": brief["context_snapshot_id"]}
    with service.store.connection() as connection:
        service.require_fresh_context(connection, ticket_id, brief["context_snapshot_id"])
        lease = connection.execute(
            "SELECT agent_id FROM leases WHERE lease_id = ?", (brief["lease_id"],)
        ).fetchone()
    assert lease is not None and lease["agent_id"] == actor.agent_id
    for skill in brief["skills"]:
        assert (REPOSITORY_ROOT / "skills" / skill / "SKILL.md").is_file(), skill
    registered = _registered_tools()
    for call in brief["next_calls"]:
        name = CALLED_TOOL.match(call)
        assert name and name.group(1) in registered, call
        assert f'ticket_id="{ticket_id}"' in call or f'ticket_ref="{ticket_id}"' in call
    if "worktree_path" in brief:
        assert Path(brief["worktree_path"]).is_dir()
    # Compact enough to pass verbatim in a delegation prompt.
    assert _estimate_tokens(brief) < 400


def test_a_developer_claim_returns_a_resolvable_brief(service, actors, seeded, tmp_path) -> None:
    service.configure_repository(actors["planner"], "project-1", str(_repository(tmp_path)))
    ticket = seeded["ticket"]

    claim = service.claim_ticket(actors["developer"], ticket["ticket_id"], ticket["version"], 900)

    brief = claim["worker_brief"]
    assert brief["role"] == "developer"
    assert brief["lease_id"] == claim["lease"]["lease_id"]
    assert brief["context_snapshot_id"] == claim["current_context_snapshot_id"]
    assert brief["skills"] == ["hoh-control-plane", "hoh-developer"]
    assert brief["next_calls"][0].startswith("dev.prepare_worktree(")
    assert "submission_id" in brief["returns"]
    _assert_resolvable(service, actors["developer"], brief)


def test_qa_and_integrator_briefs_name_the_frozen_candidate(
    service, actors, seeded, tmp_path
) -> None:
    service.configure_repository(actors["planner"], "project-1", str(_repository(tmp_path)))
    ticket = seeded["ticket"]
    developer, qa = actors["developer"], actors["qa"]
    claim = service.claim_ticket(developer, ticket["ticket_id"], ticket["version"], 900)
    lease_id = claim["lease"]["lease_id"]
    prepared = service.prepare_worktree(
        developer, ticket["ticket_id"], lease_id, claim["ticket"]["version"]
    )
    worktree = Path(prepared["worktree"]["worktree_path"])
    (worktree / "src" / "checkout").mkdir(parents=True)
    (worktree / "src" / "checkout" / "retry.txt").write_text("retry\n", encoding="utf-8")
    _git(worktree, "add", ".")
    _git(worktree, "commit", "-m", "Retry")
    packet = service.get_ticket_context(developer, ticket["ticket_id"])
    submitted = service.submit_candidate(
        developer, ticket["ticket_id"], lease_id, packet["ticket"]["version"],
        _git(worktree, "rev-parse", "HEAD"), prepared["worktree"]["base_commit"],
        prepared["worktree"]["branch_ref"], ["Ready."],
        context_snapshot_id=packet["context_snapshot_id"],
    )
    candidate_id = submitted["candidate"]["candidate_id"]

    qa_claim = service.claim_qa(qa, ticket["ticket_id"], submitted["ticket"]["version"], 900)

    brief = qa_claim["worker_brief"]
    assert brief["role"] == "qa"
    assert brief["skills"] == ["hoh-control-plane", "hoh-qa"]
    assert brief["worktree_path"] == prepared["worktree"]["worktree_path"]
    verdict_call = brief["next_calls"][-1]
    assert verdict_call.startswith("qa.submit_verdict(")
    assert f'candidate_id="{candidate_id}"' in verdict_call
    assert f'context_snapshot_id="{brief["context_snapshot_id"]}"' in verdict_call
    _assert_resolvable(service, qa, brief)

    _passed = service.submit_qa_verdict(
        qa, ticket["ticket_id"], brief["lease_id"], candidate_id,
        qa_claim["ticket"]["version"], "passed",
        [
            {
                "criterion": criterion,
                "status": "verified",
                "observation": "observed",
                "evidence": [{"type": "test_result", "observation": "ok",
                              "storage_ref": "evidence://qa/ok.json"}],
            }
            for criterion in ticket["acceptance_criteria"]
        ],
        context_snapshot_id=brief["context_snapshot_id"],
    )
    integrator = actors["integrator"]
    merge_claim = service.claim_integration(
        integrator, ticket["ticket_id"], _passed["ticket"]["version"], 900
    )

    brief = merge_claim["worker_brief"]
    assert brief["role"] == "integrator"
    assert brief["next_calls"][0].startswith("integrator.merge_candidate(")
    assert "merge_job_id" in brief["returns"]
    _assert_resolvable(service, integrator, brief)


def test_an_agent_workspace_developer_is_not_told_to_prepare_a_worktree(
    service, actors, seeded, tmp_path
) -> None:
    service.configure_repository(
        actors["planner"], "project-1", str(_repository(tmp_path)),
        workspace_model="agent_workspace",
    )
    ticket = seeded["ticket"]

    brief = service.claim_ticket(
        actors["developer"], ticket["ticket_id"], ticket["version"], 900
    )["worker_brief"]

    assert not any(call.startswith("dev.prepare_worktree") for call in brief["next_calls"])
    assert "worktree_path" not in brief
