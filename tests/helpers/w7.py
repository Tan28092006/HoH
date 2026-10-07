"""Shared setup for the wave 7 edge-case tests (tests/test_w7_*.py)."""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

from hoh_control_plane.domain import Role

REGRESSION_COMMAND = ["python", "-c", "pass"]


def git(path: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *arguments], check=True, capture_output=True, text=True
    ).stdout.strip()


def repository(tmp_path: Path, name: str = "w7-repository") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    git(repo, "init", "--initial-branch=main")
    git(repo, "config", "user.email", "hoh-tests@example.invalid")
    git(repo, "config", "user.name", "HoH Tests")
    git(repo, "config", "core.autocrlf", "false")
    (repo / "README.md").write_text("# project\n", encoding="utf-8")
    git(repo, "add", "README.md")
    git(repo, "commit", "-m", "Initial commit")
    return repo


def commit_file(repo: Path, relative: str, text: str, message: str | None = None) -> str:
    target = repo / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    git(repo, "add", relative)
    git(repo, "commit", "-m", message or f"Change {relative}")
    return git(repo, "rev-parse", "HEAD")


def ticket(service, ticket_id: str) -> dict:
    with service.store.connection() as connection:
        return service._ticket(connection, ticket_id)


def open_ticket(service, actors, ticket_id: str, scope: list[str], depends_on=None,
                feature_id: str = "payment-retry") -> dict:
    return service.create_ticket(
        actors["planner"], feature_id=feature_id, ticket_id=ticket_id,
        title=f"Work {ticket_id}", objective="Other work on the board",
        required_role=Role.DEVELOPER, acceptance_criteria=["It works"],
        preservation_constraints=["Checkout stays valid"], file_scope=scope,
        dependency_ids=depends_on or [], ready=True,
    )


def events(service, event_type: str, aggregate_id: str | None = None) -> list[dict]:
    with service.store.connection() as connection:
        rows = connection.execute(
            "SELECT * FROM audit_events WHERE event_type = ?"
            + (" AND aggregate_id = ?" if aggregate_id else "")
            + " ORDER BY sequence",
            (event_type, *((aggregate_id,) if aggregate_id else ())),
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        if isinstance(item.get("payload"), str):
            item["payload"] = json.loads(item["payload"])
        result.append(item)
    return result


def reconciliations(service, planner, status: str = "PENDING") -> list[dict]:
    return service.list_repository_reconciliations(planner, "project-1", status=status)[
        "reconciliations"
    ]


def snapshot(service, actor, ticket_id: str) -> str:
    return service.get_ticket_context(actor, ticket_id)["context_snapshot_id"]


def submit_fake(service, actors, ticket_id: str, attempt: int = 1,
                developer: str = "developer") -> dict:
    """Claim and submit a candidate in a project without a repository."""
    dev = actors[developer]
    claim = service.claim_ticket(dev, ticket_id, ticket(service, ticket_id)["version"], 900)
    return service.submit_candidate(
        dev, ticket_id, claim["lease"]["lease_id"], claim["ticket"]["version"],
        f"{attempt:x}" * 40, "b" * 40, f"agent/{dev.agent_id}/{ticket_id}", ["Ready."],
        context_snapshot_id=snapshot(service, dev, ticket_id),
    )


def finding(criterion: str, status: str = "verified", evidence_type: str = "test_result",
            storage_ref: str = "evidence://qa/run.json") -> dict:
    return {
        "criterion": criterion, "status": status,
        "severity": "none" if status == "verified" else "high",
        "observation": f"{criterion}: {status}",
        "evidence": [{"type": evidence_type, "observation": status, "storage_ref": storage_ref}],
    }


def verdict(service, actors, ticket_id: str, outcome: str, findings: list[dict],
            qa: str = "qa") -> dict:
    """Claim QA on the current candidate and submit one verdict."""
    actor = actors[qa]
    claim = service.claim_qa(actor, ticket_id, ticket(service, ticket_id)["version"], 900)
    with service.store.connection() as connection:
        candidate_id = connection.execute(
            "SELECT candidate_id FROM candidates WHERE ticket_id = ? "
            "ORDER BY submitted_at DESC LIMIT 1", (ticket_id,),
        ).fetchone()["candidate_id"]
    return service.submit_qa_verdict(
        actor, ticket_id, claim["lease"]["lease_id"], candidate_id,
        claim["ticket"]["version"], outcome, findings,
        context_snapshot_id=claim["current_context_snapshot_id"],
    )


def pass_all(service, actors, ticket_id: str, extra: list[dict] | None = None,
             evidence_type: str = "test_result") -> dict:
    criteria = ticket(service, ticket_id)["acceptance_criteria"]
    return verdict(
        service, actors, ticket_id, "passed",
        [finding(c, evidence_type=evidence_type, storage_ref=f"evidence://qa/{i}.json")
         for i, c in enumerate(criteria)] + (extra or []),
    )


def record_regression_run(service, candidate_id: str, ticket_id: str, exit_code: int = 0) -> None:
    digest = hashlib.sha256("\0".join(REGRESSION_COMMAND).encode()).hexdigest()
    with service.store.connection(immediate=True) as connection:
        connection.execute(
            """
            INSERT INTO qa_test_runs
                (test_run_id, candidate_id, ticket_id, command_digest, exit_code, timed_out,
                 duration_ms, storage_ref, report_path, executed_by, created_at)
            VALUES (?, ?, ?, ?, ?, 0, 10, 'evidence://local/run.json', 'run.json', 'qa-01', ?)
            """,
            (f"testrun_{candidate_id[-10:]}_{exit_code}", candidate_id, ticket_id, digest,
             exit_code, "2026-01-01T00:00:00Z"),
        )


def current_candidate(service, ticket_id: str) -> dict:
    with service.store.connection() as connection:
        t = service._ticket(connection, ticket_id)
        row = connection.execute(
            "SELECT * FROM candidates WHERE ticket_id = ? AND commit_sha = ? "
            "ORDER BY submitted_at DESC LIMIT 1", (ticket_id, t["candidate_commit"]),
        ).fetchone()
    return dict(row)


def verified_with_repository(service, actors, seeded, tmp_path: Path, *,
                             files: dict[str, str] | None = None,
                             extra_findings: list[dict] | None = None,
                             repo: Path | None = None,
                             ticket_id: str | None = None) -> dict:
    """A real repository, one candidate changing ``files``, QA passed and a regression run."""
    planner = actors["planner"]
    if repo is None:
        repo = repository(tmp_path)
        service.configure_repository(planner, "project-1", str(repo))
        service.configure_test_command(planner, "project-1", REGRESSION_COMMAND, 60)
    ticket_id = ticket_id or seeded["ticket"]["ticket_id"]
    developer = actors["developer"]
    claim = service.claim_ticket(developer, ticket_id, ticket(service, ticket_id)["version"], 900)
    prepared = service.prepare_worktree(
        developer, ticket_id, claim["lease"]["lease_id"], claim["ticket"]["version"],
    )
    worktree = Path(prepared["worktree"]["worktree_path"])
    for relative, text in (files or {"src/checkout/retry.txt": "safe retry\n"}).items():
        path = worktree / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        git(worktree, "add", relative)
    git(worktree, "commit", "-m", "Candidate change")
    submitted = service.submit_candidate(
        developer, ticket_id, claim["lease"]["lease_id"], claim["ticket"]["version"],
        git(worktree, "rev-parse", "HEAD"), prepared["worktree"]["base_commit"],
        prepared["worktree"]["branch_ref"], ["Ready for QA."],
        context_snapshot_id=snapshot(service, developer, ticket_id),
    )
    verified = pass_all(service, actors, ticket_id, extra=extra_findings)
    record_regression_run(service, submitted["candidate"]["candidate_id"], ticket_id)
    return {"repository": repo, "ticket_id": ticket_id, "candidate": submitted["candidate"],
            "verified": verified, "worktree": worktree}


def claim_integration(service, actors, ticket_id: str) -> dict:
    return service.claim_integration(
        actors["integrator"], ticket_id, ticket(service, ticket_id)["version"], 900
    )


def queue_merge(service, actors, ticket_id: str, integration: dict) -> dict:
    return service.queue_merge_candidate(
        actors["integrator"], ticket_id, integration["lease"]["lease_id"],
        integration["ticket"]["version"],
        context_snapshot_id=snapshot(service, actors["integrator"], ticket_id),
    )
