"""Wave 8A, R9: a passed verdict means every finding was verified, on real bytes.

Before 8A a passed verdict was accepted beside a failed finding, and the
ticket then stuck at the merge gate (unverified_qa_finding); a local evidence
reference with no file behind it was recorded as a claim.
"""
from __future__ import annotations

import sys

import pytest

from hoh_control_plane.domain import ValidationError
from test_qa_runner import _candidate_waiting_for_qa


def _finding(criterion: str, status: str, storage_ref: str = "evidence://qa/run.json") -> dict:
    return {
        "criterion": criterion, "status": status, "severity": "none" if status == "verified" else "high",
        "observation": f"{status} against the frozen candidate.",
        "evidence": [{"type": "test_result", "observation": status, "storage_ref": storage_ref}],
    }


def _submit(service, actors, ticket, candidate, claim, verdict, findings):
    return service.submit_qa_verdict(
        actors["qa"], ticket["ticket_id"], claim["lease"]["lease_id"],
        candidate["candidate"]["candidate_id"], claim["ticket"]["version"], verdict, findings,
        context_snapshot_id=service.get_ticket_context(
            actors["qa"], ticket["ticket_id"])["context_snapshot_id"])


def _state(service, ticket_id, candidate_id):
    with service.store.connection() as connection:
        status = service._ticket(connection, ticket_id)["status"]
        findings = connection.execute(
            "SELECT COUNT(*) FROM qa_findings WHERE candidate_id = ?", (candidate_id,)).fetchone()[0]
        evidence = connection.execute(
            "SELECT COUNT(*) FROM evidence WHERE candidate_id = ?", (candidate_id,)).fetchone()[0]
    return status, findings, evidence


@pytest.fixture
def waiting(service, actors, seeded, tmp_path):
    return _candidate_waiting_for_qa(service, actors, seeded, tmp_path,
                                     [sys.executable, "-c", "print('passed')"])


@pytest.mark.parametrize("other_status", ["failed", "insufficient_evidence"])
def test_a_passed_verdict_with_any_unverified_finding_is_refused(
    service, actors, waiting, other_status
):
    ticket, candidate, claim = waiting
    second = ticket["acceptance_criteria"][1]
    # Every criterion has a verified finding, so the old rule accepted this.
    findings = [_finding(c, "verified") for c in ticket["acceptance_criteria"]]
    findings.append(_finding(second, other_status))
    with pytest.raises(ValidationError) as refused:
        _submit(service, actors, ticket, candidate, claim, "passed", findings)
    assert refused.value.details == {
        "reason": "passed_with_unverified_finding",
        "findings": [{"criterion": second, "status": other_status}],
    }
    assert _state(service, ticket["ticket_id"], candidate["candidate"]["candidate_id"]) == (
        "QA_IN_PROGRESS", 0, 0)

    # The same findings are a legitimate rejection.
    rejected = _submit(service, actors, ticket, candidate, claim, "failed", findings)
    assert rejected["ticket"]["status"] in {"REWORK", "BLOCKED"}


def test_an_all_verified_passed_verdict_still_verifies(service, actors, waiting):
    ticket, candidate, claim = waiting
    verified = _submit(service, actors, ticket, candidate, claim, "passed",
                       [_finding(c, "verified") for c in ticket["acceptance_criteria"]])
    assert verified["ticket"]["status"] == "VERIFIED"


@pytest.mark.parametrize("verdict", ["passed", "failed"])
def test_a_local_reference_with_no_file_is_refused(service, actors, waiting, verdict):
    ticket, candidate, claim = waiting
    candidate_id = candidate["candidate"]["candidate_id"]
    missing = f"evidence://local/{candidate_id}/missing.json"
    status = "verified" if verdict == "passed" else "failed"
    findings = [_finding(c, status, missing) for c in ticket["acceptance_criteria"]]
    with pytest.raises(ValidationError) as refused:
        _submit(service, actors, ticket, candidate, claim, verdict, findings)
    assert refused.value.details == {"reason": "evidence_artifact_missing", "storage_ref": missing}
    assert _state(service, ticket["ticket_id"], candidate_id) == ("QA_IN_PROGRESS", 0, 0)


def test_a_local_reference_to_real_bytes_is_checksummed_from_them(service, actors, waiting):
    ticket, candidate, claim = waiting
    candidate_id = candidate["candidate"]["candidate_id"]
    report = service.project_tests.evidence_root / candidate_id / "run.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text('{"termination_reason": "passed"}', encoding="utf-8")
    ref = f"evidence://local/{candidate_id}/run.json"
    verified = _submit(service, actors, ticket, candidate, claim, "passed",
                       [_finding(c, "verified", ref) for c in ticket["acceptance_criteria"]])
    assert verified["ticket"]["status"] == "VERIFIED"
    with service.store.connection() as connection:
        sources = {row[0] for row in connection.execute(
            "SELECT checksum_source FROM evidence WHERE candidate_id = ?", (candidate_id,))}
    assert sources == {"artifact"}
