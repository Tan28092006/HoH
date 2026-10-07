from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

import pytest

from hoh_control_plane.domain import ValidationError
from hoh_control_plane.service import ControlPlaneService
from hoh_control_plane.store import SQLiteStore
from helpers.qa import _git
from helpers.qa import candidate_waiting_for_qa as _candidate_waiting_for_qa


def _verdict(service, actors, ticket, candidate, qa_claim, evidence: dict[str, object]):
    return service.submit_qa_verdict(
        actors["qa"],
        ticket["ticket_id"],
        qa_claim["lease"]["lease_id"],
        candidate["candidate"]["candidate_id"],
        qa_claim["ticket"]["version"],
        "failed",
        [
            {
                "criterion": ticket["acceptance_criteria"][0],
                "status": "failed",
                "severity": "high",
                "observation": "The retry still duplicates an order.",
                "evidence": [evidence],
            }
        ],
        context_snapshot_id=service.get_ticket_context(actors["qa"], ticket["ticket_id"])["context_snapshot_id"],
    )


def _stored_evidence(service, candidate_id: str) -> list[dict[str, object]]:
    with service.store.connection() as connection:
        return [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM evidence WHERE candidate_id = ?", (candidate_id,)
            ).fetchall()
        ]


def test_every_evidence_row_records_its_provenance(
    service, actors, seeded, tmp_path: Path
) -> None:
    ticket, candidate, qa_claim = _candidate_waiting_for_qa(service, actors, seeded, tmp_path)

    _verdict(
        service, actors, ticket, candidate, qa_claim,
        {
            "type": "test_result",
            "observation": "duplicate-order regression reproduced",
            "storage_ref": "evidence://qa/duplicate-order.json",
            "scenario_ref": "scenario:retry-duplicate-order",
            "environment_digest": "a" * 64,
            "storage_checksum": "b" * 64,
        },
    )

    stored = _stored_evidence(service, candidate["candidate"]["candidate_id"])[0]

    assert stored["candidate_id"] == candidate["candidate"]["candidate_id"]
    assert stored["criterion"] == ticket["acceptance_criteria"][0]
    assert stored["scenario_ref"] == "scenario:retry-duplicate-order"
    assert stored["environment_digest"] == "a" * 64
    assert stored["storage_checksum"] == "b" * 64
    assert stored["created_by"] == actors["qa"].agent_id
    assert stored["created_at"].endswith("Z")


def test_no_persisted_evidence_row_is_left_without_provenance(
    service, actors, seeded, tmp_path: Path
) -> None:
    """A QA item that names none of the provenance still records all of it."""
    ticket, candidate, qa_claim = _candidate_waiting_for_qa(service, actors, seeded, tmp_path)

    _verdict(
        service, actors, ticket, candidate, qa_claim,
        {
            "type": "test_result",
            "observation": "duplicate-order regression reproduced",
            "storage_ref": "evidence://qa/duplicate-order.json",
        },
    )

    stored = _stored_evidence(service, candidate["candidate"]["candidate_id"])[0]

    assert stored["scenario_ref"] == f"criterion:{ticket['acceptance_criteria'][0]}"
    assert stored["environment_digest"]
    assert stored["storage_checksum"]
    assert stored["created_by"] and stored["created_at"]


def test_an_observation_recorded_after_a_run_inherits_that_run_environment(
    service, actors, seeded, tmp_path: Path
) -> None:
    ticket, candidate, qa_claim = _candidate_waiting_for_qa(service, actors, seeded, tmp_path)
    run = service.run_project_tests(
        actors["qa"], ticket["ticket_id"], qa_claim["lease"]["lease_id"]
    )["test_run"]

    _verdict(
        service, actors, ticket, candidate, qa_claim,
        {
            "type": "test_result",
            "observation": "seen",
            "storage_ref": "evidence://qa/manual.json",
        },
    )

    stored = _stored_evidence(service, candidate["candidate"]["candidate_id"])[0]
    with service.store.connection() as connection:
        recorded_run = dict(
            connection.execute(
                "SELECT environment_digest FROM qa_test_runs WHERE test_run_id = ?",
                (run["test_run_id"],),
            ).fetchone()
        )

    assert stored["environment_digest"] == recorded_run["environment_digest"]


def test_a_runner_report_checksum_matches_the_bytes_on_disk(
    service, actors, seeded, tmp_path: Path
) -> None:
    """The digest must describe the stored file, not the text before it was written."""
    ticket, candidate, qa_claim = _candidate_waiting_for_qa(service, actors, seeded, tmp_path)

    run = service.run_project_tests(
        actors["qa"], ticket["ticket_id"], qa_claim["lease"]["lease_id"]
    )["test_run"]
    with service.store.connection() as connection:
        stored = dict(
            connection.execute(
                "SELECT report_path, storage_ref, storage_checksum FROM qa_test_runs"
                " WHERE test_run_id = ?",
                (run["test_run_id"],),
            ).fetchone()
        )
    raw = Path(stored["report_path"]).read_bytes()

    assert stored["storage_checksum"] == hashlib.sha256(raw).hexdigest()
    # The same artifact must survive the validation path QA evidence goes through.
    verified, source = service._verified_storage_checksum(
        stored["storage_ref"],
        candidate["candidate"]["candidate_id"],
        stored["storage_checksum"],
        "seen",
    )
    assert verified == stored["storage_checksum"]
    assert source == "artifact", "read from the bytes, not taken on the agent's word"


def test_a_runner_artifact_is_accepted_as_qa_evidence_without_a_mismatch(
    service, actors, seeded, tmp_path: Path
) -> None:
    """End to end: a report the runner wrote can be cited by a verdict."""
    ticket, candidate, qa_claim = _candidate_waiting_for_qa(service, actors, seeded, tmp_path)
    run = service.run_project_tests(
        actors["qa"], ticket["ticket_id"], qa_claim["lease"]["lease_id"]
    )["test_run"]

    _verdict(
        service, actors, ticket, candidate, qa_claim,
        {
            "type": "test_result",
            "observation": "regression suite completed",
            "storage_ref": run["storage_ref"],
            "storage_checksum": hashlib.sha256(
                Path(run["report_path"]).read_bytes()
            ).hexdigest(),
        },
    )

    stored = _stored_evidence(service, candidate["candidate"]["candidate_id"])[0]

    assert stored["storage_ref"] == run["storage_ref"]
    assert stored["storage_checksum"] == hashlib.sha256(
        Path(run["report_path"]).read_bytes()
    ).hexdigest()


@pytest.mark.parametrize(
    "storage_ref",
    [
        "https://example.invalid/report.json",
        "evidence://",
        "evidence://qa/../../secrets.json",
        "  ",
    ],
)
def test_an_invalid_storage_reference_fails_closed(
    service, actors, seeded, tmp_path: Path, storage_ref: str
) -> None:
    ticket, candidate, qa_claim = _candidate_waiting_for_qa(service, actors, seeded, tmp_path)

    with pytest.raises(ValidationError) as excinfo:
        _verdict(
            service, actors, ticket, candidate, qa_claim,
            {"type": "test_result", "observation": "seen", "storage_ref": storage_ref},
        )

    assert excinfo.value.details["reason"] == "evidence_storage_ref_invalid"
    assert _stored_evidence(service, candidate["candidate"]["candidate_id"]) == []


def test_a_storage_reference_is_canonicalized_before_it_is_stored(
    service, actors, seeded, tmp_path: Path
) -> None:
    ticket, candidate, qa_claim = _candidate_waiting_for_qa(service, actors, seeded, tmp_path)

    _verdict(
        service, actors, ticket, candidate, qa_claim,
        {
            "type": "test_result",
            "observation": "seen",
            "storage_ref": "  evidence://qa\\\\nested//run.json  ",
        },
    )

    stored = _stored_evidence(service, candidate["candidate"]["candidate_id"])[0]

    assert stored["storage_ref"] == "evidence://qa/nested/run.json"


def test_a_local_artifact_belonging_to_another_candidate_is_refused(
    service, actors, seeded, tmp_path: Path
) -> None:
    ticket, candidate, qa_claim = _candidate_waiting_for_qa(service, actors, seeded, tmp_path)

    with pytest.raises(ValidationError) as excinfo:
        _verdict(
            service, actors, ticket, candidate, qa_claim,
            {
                "type": "test_result",
                "observation": "seen",
                "storage_ref": "evidence://local/cand_someone_else/run.json",
            },
        )

    assert excinfo.value.details["reason"] == "evidence_candidate_mismatch"
    assert _stored_evidence(service, candidate["candidate"]["candidate_id"]) == []


def test_a_checksum_that_disagrees_with_the_artifact_fails_closed(
    service, actors, seeded, tmp_path: Path
) -> None:
    ticket, candidate, qa_claim = _candidate_waiting_for_qa(service, actors, seeded, tmp_path)
    candidate_id = candidate["candidate"]["candidate_id"]
    artifact = service.project_tests.evidence_root / candidate_id / "run.json"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text('{"result": "passed"}', encoding="utf-8")

    with pytest.raises(ValidationError) as excinfo:
        _verdict(
            service, actors, ticket, candidate, qa_claim,
            {
                "type": "test_result",
                "observation": "seen",
                "storage_ref": f"evidence://local/{candidate_id}/run.json",
                "storage_checksum": "c" * 64,
            },
        )

    assert excinfo.value.details["reason"] == "evidence_checksum_mismatch"
    assert _stored_evidence(service, candidate_id) == []


def test_a_local_artifact_is_checksummed_from_its_own_bytes(
    service, actors, seeded, tmp_path: Path
) -> None:
    ticket, candidate, qa_claim = _candidate_waiting_for_qa(service, actors, seeded, tmp_path)
    candidate_id = candidate["candidate"]["candidate_id"]
    artifact = service.project_tests.evidence_root / candidate_id / "run.json"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    body = '{"result": "failed"}'
    artifact.write_text(body, encoding="utf-8")

    _verdict(
        service, actors, ticket, candidate, qa_claim,
        {
            "type": "test_result",
            "observation": "seen",
            "storage_ref": f"evidence://local/{candidate_id}/run.json",
        },
    )

    stored = _stored_evidence(service, candidate_id)[0]

    assert stored["storage_checksum"] == hashlib.sha256(body.encode("utf-8")).hexdigest()


def test_a_completed_run_records_its_environment_and_artifact_checksum(
    service, actors, seeded, tmp_path: Path
) -> None:
    ticket, candidate, qa_claim = _candidate_waiting_for_qa(service, actors, seeded, tmp_path)

    result = service.run_project_tests(
        actors["qa"], ticket["ticket_id"], qa_claim["lease"]["lease_id"]
    )
    run = result["test_run"]
    with service.store.connection() as connection:
        stored = dict(
            connection.execute(
                "SELECT * FROM qa_test_runs WHERE test_run_id = ?", (run["test_run_id"],)
            ).fetchone()
        )

    body = Path(stored["report_path"]).read_text(encoding="utf-8")
    assert stored["environment_digest"]
    assert len(stored["environment_digest"]) == 64
    assert stored["storage_checksum"] == hashlib.sha256(body.encode("utf-8")).hexdigest()
    assert stored["candidate_id"] == candidate["candidate"]["candidate_id"]


def test_the_environment_digest_separates_environments_but_not_runs(
    service, actors, seeded, tmp_path: Path
) -> None:
    """Two runs of the same command in the same environment share a digest."""
    runner = service.project_tests
    command = [sys.executable, "-c", "print('x')"]

    same = runner.environment_digest(command, "/worktree/src/hoh_control_plane/__init__.py")
    repeat = runner.environment_digest(command, "/worktree/src/hoh_control_plane/__init__.py")
    other_source = runner.environment_digest(command, "/elsewhere/src/hoh_control_plane/__init__.py")
    other_command = runner.environment_digest(
        [sys.executable, "-c", "print('y')"], "/worktree/src/hoh_control_plane/__init__.py"
    )

    assert same == repeat
    assert same != other_source
    assert same != other_command


def test_ticket_context_summarizes_provenance_without_copying_the_log(
    service, actors, seeded, tmp_path: Path
) -> None:
    ticket, _candidate, qa_claim = _candidate_waiting_for_qa(service, actors, seeded, tmp_path)
    service.run_project_tests(actors["qa"], ticket["ticket_id"], qa_claim["lease"]["lease_id"])

    context = service.get_ticket_context(actors["qa"], ticket["ticket_id"])
    run = context["test_runs"][0]
    rendered = str(context)

    assert run["storage_ref"].startswith("evidence://local/")
    assert run["environment_digest"]
    assert run["storage_checksum"]
    # The report body stays behind its reference rather than being inlined.
    assert "stdout" not in run
    assert "passed\n" not in rendered


def test_migration_preserves_existing_evidence_and_its_foreign_keys(tmp_path: Path) -> None:
    """A database written before provenance existed keeps every row and reference."""
    database = tmp_path / "legacy.db"
    store = SQLiteStore(database)
    store.initialize()
    with store.connection(immediate=True) as connection:
        # Rebuild the pre-provenance shape of the two tables, with data in them.
        connection.execute("DROP TABLE evidence")
        connection.execute(
            """
            CREATE TABLE evidence (
                evidence_id TEXT PRIMARY KEY,
                candidate_id TEXT NOT NULL REFERENCES candidates(candidate_id),
                criterion TEXT NOT NULL,
                evidence_type TEXT NOT NULL,
                observation TEXT NOT NULL,
                storage_ref TEXT NOT NULL,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO projects (project_id, name, description, default_branch,
                                  created_at, updated_at)
            VALUES ('legacy', 'Legacy', '', 'main', '2026-01-01T00:00:00Z',
                    '2026-01-01T00:00:00Z')
            """
        )
        connection.execute(
            """
            INSERT INTO features (feature_id, project_id, title, objective,
                                  created_at, updated_at)
            VALUES ('legacy-feature', 'legacy', 'Legacy', 'Objective',
                    '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')
            """
        )
        connection.execute(
            """
            INSERT INTO tickets (ticket_id, feature_id, title, objective, status,
                                 required_role, acceptance_criteria,
                                 preservation_constraints, file_scope, created_at, updated_at)
            VALUES ('LEGACY-1', 'legacy-feature', 'Legacy', 'Objective', 'DONE',
                    'developer', '[]', '[]', '[]', '2026-01-01T00:00:00Z',
                    '2026-01-01T00:00:00Z')
            """
        )
        connection.execute(
            """
            INSERT INTO candidates
                (candidate_id, ticket_id, base_commit, commit_sha, branch_ref,
                 developer_claims, submitted_by, submitted_at, frozen_at)
            VALUES ('cand_legacy', 'LEGACY-1', 'a', 'b', 'branch', '[]', 'dev-01',
                    '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')
            """
        )
        connection.execute(
            """
            INSERT INTO evidence
                (evidence_id, candidate_id, criterion, evidence_type, observation,
                 storage_ref, created_by, created_at)
            VALUES ('ev_legacy', 'cand_legacy', 'Old criterion', 'test_result',
                    'historic observation', 'evidence://qa/old.json', 'qa-01',
                    '2026-01-01T00:00:00Z')
            """
        )

    SQLiteStore(database).initialize()

    with store.connection() as connection:
        preserved = dict(
            connection.execute(
                "SELECT * FROM evidence WHERE evidence_id = 'ev_legacy'"
            ).fetchone()
        )
        foreign_keys = {
            row["table"]
            for row in connection.execute("PRAGMA foreign_key_list(evidence)")
        }

    assert preserved["observation"] == "historic observation"
    assert preserved["storage_ref"] == "evidence://qa/old.json"
    # Provenance is absent rather than invented for a row written before it existed.
    assert preserved["scenario_ref"] is None
    assert preserved["environment_digest"] is None
    assert preserved["storage_checksum"] is None
    assert "candidates" in foreign_keys


def test_the_migration_is_safe_to_run_again(tmp_path: Path) -> None:
    database = tmp_path / "repeat.db"
    SQLiteStore(database).initialize()
    SQLiteStore(database).initialize()
    SQLiteStore(database).initialize()

    with SQLiteStore(database).connection() as connection:
        columns = [row["name"] for row in connection.execute("PRAGMA table_info(evidence)")]

    assert columns.count("scenario_ref") == 1
    assert columns.count("environment_digest") == 1
    assert columns.count("storage_checksum") == 1


def test_a_developer_claim_is_never_stored_as_evidence(
    service, actors, seeded, tmp_path: Path
) -> None:
    """Provenance columns must not become a way to launder a Developer assertion."""
    ticket, candidate, _qa_claim = _candidate_waiting_for_qa(service, actors, seeded, tmp_path)

    stored = _stored_evidence(service, candidate["candidate"]["candidate_id"])

    assert stored == []
    assert candidate["candidate"]["developer_claims"] == ["Ready for QA."]
    assert isinstance(service, ControlPlaneService)


# --- telling a proved artifact from a claimed one --------------------------------


def test_a_claimed_digest_is_not_recorded_as_a_proved_artifact(
    service, actors, seeded
) -> None:
    """Section 25: no verdict may hang off an artifact nobody identified.

    The two cases are indistinguishable once written, so which one it was has to
    survive in the row -- otherwise storage that stopped reaching this machine
    silently downgrades every verdict and nothing says so.
    """
    checksum, source = service._verified_storage_checksum(
        "evidence://remote/somewhere/report.json", "cand-1", None, "I saw it pass"
    )

    assert checksum, "a row is never left without a checksum"
    assert source == "claimed", "nothing here read those bytes"


def test_a_local_artifact_that_is_not_on_this_machine_is_refused(
    service, actors, seeded
) -> None:
    """Wave 8A, R9: evidence://local/ names this host's store; no bytes, no evidence.

    Before 8A this read as a claim, which let a passed verdict cite a report
    that never existed.
    """
    with pytest.raises(ValidationError) as refused:
        service._verified_storage_checksum(
            "evidence://local/cand-9/never-written.json", "cand-9", None, "trust me"
        )

    assert refused.value.details == {
        "reason": "evidence_artifact_missing",
        "storage_ref": "evidence://local/cand-9/never-written.json",
    }


def test_the_metric_shows_how_much_evidence_is_actually_backed(
    service, store, actors, seeded
) -> None:
    from hoh_control_plane.observability import WorkflowMetrics

    metrics = WorkflowMetrics(store).project_metrics(actors["planner"], "project-1")

    evidence = metrics["evidence"]
    assert set(evidence) >= {
        "artifact_backed_items",
        "claimed_items",
        "artifact_backed_ratio",
    }
    assert evidence["artifact_backed_items"] + evidence["claimed_items"] == (
        evidence["evidence_items"]
    ), "every item is one or the other"


def test_an_echoed_short_checksum_of_the_artifact_is_accepted_and_stored_in_full(
    service, actors, seeded, tmp_path: Path
) -> None:
    """Agents are shown 12-character digests; echoing one back must not fail the verdict."""
    ticket, candidate, qa_claim = _candidate_waiting_for_qa(service, actors, seeded, tmp_path)
    run = service.run_project_tests(
        actors["qa"], ticket["ticket_id"], qa_claim["lease"]["lease_id"]
    )["test_run"]
    full = hashlib.sha256(Path(run["report_path"]).read_bytes()).hexdigest()

    _verdict(
        service, actors, ticket, candidate, qa_claim,
        {
            "type": "test_result",
            "observation": "regression suite completed",
            "storage_ref": run["storage_ref"],
            "storage_checksum": full[:12],
        },
    )

    stored = _stored_evidence(service, candidate["candidate"]["candidate_id"])[0]
    assert stored["storage_checksum"] == full


def test_a_short_prefix_of_another_digest_still_fails_closed(
    service, actors, seeded, tmp_path: Path
) -> None:
    ticket, candidate, qa_claim = _candidate_waiting_for_qa(service, actors, seeded, tmp_path)
    candidate_id = candidate["candidate"]["candidate_id"]
    artifact = service.project_tests.evidence_root / candidate_id / "run.json"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text('{"result": "passed"}', encoding="utf-8")
    full = hashlib.sha256(artifact.read_bytes()).hexdigest()
    wrong = ("0" if full[0] != "0" else "1") + full[1:12]

    for supplied in (wrong, full[:8]):
        with pytest.raises(ValidationError) as excinfo:
            _verdict(
                service, actors, ticket, candidate, qa_claim,
                {
                    "type": "test_result",
                    "observation": "seen",
                    "storage_ref": f"evidence://local/{candidate_id}/run.json",
                    "storage_checksum": supplied,
                },
            )
        assert excinfo.value.details["reason"] == "evidence_checksum_mismatch"
    assert _stored_evidence(service, candidate_id) == []
