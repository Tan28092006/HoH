"""Wave 8A, R4: maker-checker by identity, and credentials no file reveals.

The agent that submitted a candidate never claims it for QA or integration,
judges it or merges it, whatever role its credential carries now. The local
credential file holds sha256(token) only.
"""
from __future__ import annotations

import json

import pytest

from hoh_control_plane.auth import (
    AgentCredential,
    credentials_from_environment,
    resolve_actor,
    token_digest,
)
from hoh_control_plane.domain import Actor, AuthorizationError, Role
from hoh_control_plane.local_credentials import (
    credential_provider,
    register_local_credential,
    token_env_var,
)
from helpers.integration import (
    _candidate_ready_for_integration,
    _claimed_for_integration,
)
from helpers.qa import candidate_waiting_for_qa_run as _candidate_waiting_for_qa


def _as(agent_id: str, role: Role) -> Actor:
    return Actor(agent_id, role, frozenset({"project-1"}))


def _set_submitter(service, candidate_id: str, agent_id: str) -> None:
    with service.store.connection(immediate=True) as connection:
        connection.execute("UPDATE candidates SET submitted_by = ? WHERE candidate_id = ?",
                           (agent_id, candidate_id))


def test_the_submitter_cannot_claim_its_own_candidate_for_qa(service, actors, seeded, tmp_path):
    import sys

    ticket, candidate, claim = _candidate_waiting_for_qa(
        service, actors, seeded, tmp_path, [sys.executable, "-c", "print('passed')"])
    # Reproduce QA_READY without the lease, then try the claim as dev-01 holding QA.
    with service.store.connection(immediate=True) as connection:
        connection.execute("DELETE FROM leases WHERE ticket_id = ?", (ticket["ticket_id"],))
        connection.execute("UPDATE tickets SET status = 'QA_READY' WHERE ticket_id = ?",
                           (ticket["ticket_id"],))
        version = service._ticket(connection, ticket["ticket_id"])["version"]
    with pytest.raises(AuthorizationError) as refused:
        service.claim_qa(_as("dev-01", Role.QA), ticket["ticket_id"], version, 900)
    assert refused.value.status_code == 403
    assert refused.value.details["reason"] == "self_review"
    assert refused.value.details["submitted_by"] == "dev-01"
    assert refused.value.details["candidate_id"] == candidate["candidate"]["candidate_id"]
    # Any other QA identity still claims it.
    assert service.claim_qa(actors["qa"], ticket["ticket_id"], version, 900)["lease"]


def test_a_verdict_from_the_submitter_is_refused_at_the_write(service, actors, seeded, tmp_path):
    import sys

    ticket, candidate, claim = _candidate_waiting_for_qa(
        service, actors, seeded, tmp_path, [sys.executable, "-c", "print('passed')"])
    # The QA agent's identity is the candidate's maker (claim taken before 8A).
    _set_submitter(service, candidate["candidate"]["candidate_id"], "qa-01")
    findings = [{
        "criterion": criterion, "status": "verified", "severity": "none",
        "observation": "Verified.",
        "evidence": [{"type": "test_result", "observation": "passed",
                      "storage_ref": f"evidence://qa/{index}.json"}],
    } for index, criterion in enumerate(ticket["acceptance_criteria"], start=1)]
    with pytest.raises(AuthorizationError) as refused:
        service.submit_qa_verdict(
            actors["qa"], ticket["ticket_id"], claim["lease"]["lease_id"],
            candidate["candidate"]["candidate_id"], claim["ticket"]["version"], "passed", findings,
            context_snapshot_id=service.get_ticket_context(
                actors["qa"], ticket["ticket_id"])["context_snapshot_id"])
    assert refused.value.details["reason"] == "self_review"
    with service.store.connection() as connection:
        assert service._ticket(connection, ticket["ticket_id"])["status"] == "QA_IN_PROGRESS"


def test_the_submitter_cannot_claim_or_merge_as_integrator(service, actors, seeded, tmp_path):
    _repository, ticket, candidate, verified = _candidate_ready_for_integration(
        service, actors, seeded, tmp_path)
    with pytest.raises(AuthorizationError) as refused:
        service.claim_integration(_as("dev-01", Role.INTEGRATOR), ticket["ticket_id"],
                                  verified["ticket"]["version"], 900)
    assert refused.value.details["reason"] == "self_review"

    integration = _claimed_for_integration(service, actors, ticket, verified)
    _set_submitter(service, candidate["candidate"]["candidate_id"], "integrator-01")
    with pytest.raises(AuthorizationError) as refused:
        service.queue_merge_candidate(
            actors["integrator"], ticket["ticket_id"], integration["lease"]["lease_id"],
            integration["ticket"]["version"],
            context_snapshot_id=service.get_ticket_context(
                actors["integrator"], ticket["ticket_id"])["context_snapshot_id"])
    assert refused.value.details["reason"] == "self_review"
    with service.store.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM merge_jobs").fetchone()[0] == 0


def test_a_server_rebased_candidate_is_still_its_original_makers_work(service, actors, seeded, tmp_path):
    _repository, ticket, candidate, _verified = _candidate_ready_for_integration(
        service, actors, seeded, tmp_path)
    original = candidate["candidate"]
    with service.store.connection(immediate=True) as connection:
        # Two rebases recorded by the Integrator: the maker is still dev-01.
        for derived_id, parent in (("cand_derived1", original["candidate_id"]),
                                   ("cand_derived2", "cand_derived1")):
            connection.execute(
                """INSERT INTO candidates (candidate_id, ticket_id, base_commit, commit_sha,
                       branch_ref, developer_claims, submitted_by, submitted_at, frozen_at,
                       derived_from)
                   VALUES (?, ?, ?, ?, 'refs/hoh/rebased/x', '[]', 'integrator-01',
                           '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', ?)""",
                (derived_id, ticket["ticket_id"], original["base_commit"],
                 derived_id.ljust(40, "0"), parent))
        derived = dict(connection.execute(
            "SELECT * FROM candidates WHERE candidate_id = 'cand_derived2'").fetchone())
        assert service._candidate_maker_id(connection, derived) == "dev-01"
        # The Integrator that recorded the rebase is not refused ...
        service._refuse_self_review(connection, actors["integrator"], derived, "merge")
        # ... the Developer acting as Integrator is.
        with pytest.raises(AuthorizationError) as refused:
            service._refuse_self_review(connection, _as("dev-01", Role.INTEGRATOR), derived, "merge")
        assert refused.value.details["submitted_by"] == "dev-01"
        # No candidate (nothing frozen yet) refuses nothing.
        service._refuse_self_review(connection, actors["qa"], None, "claim")


# --- credentials ------------------------------------------------------------

def _actor(role: str = "qa") -> Actor:
    return Actor(f"local-{role}-abcd1234", Role(role), frozenset({"shop"}))


def test_a_new_credential_is_stored_only_as_its_digest(tmp_path):
    token = register_local_credential(tmp_path, _actor())
    raw = (tmp_path / "config/agent-credentials.json").read_text(encoding="utf-8")
    assert token and token not in raw
    assert json.loads(raw) == {token_digest(token): {
        "agent_id": "local-qa-abcd1234", "role": "qa", "project_ids": ["shop"]}}
    credentials = credentials_from_environment(raw)
    assert credentials[0].hashed
    assert resolve_actor(credentials, f"Bearer {token}") == _actor()
    # The digest itself is not a credential.
    with pytest.raises(AuthorizationError):
        resolve_actor(credentials, f"Bearer {token_digest(token)}")
    with pytest.raises(AuthorizationError):
        resolve_actor(credentials, f"Bearer {token}x")
    # Re-registering keeps the issued token valid and reveals nothing.
    assert register_local_credential(tmp_path, _actor()) is None
    assert (tmp_path / "config/agent-credentials.json").read_text(encoding="utf-8") == raw


def test_a_pre_8a_plaintext_entry_is_rekeyed_without_breaking_its_client(tmp_path):
    path = tmp_path / "config/agent-credentials.json"
    path.parent.mkdir(parents=True)
    legacy = "L" * 64
    other = "O" * 64
    path.write_text(json.dumps({
        legacy: {"agent_id": "local-qa-abcd1234", "role": "qa", "project_ids": ["shop"]},
        other: {"agent_id": "local-developer-abcd1234", "role": "developer", "project_ids": ["shop"]},
    }), encoding="utf-8")
    assert register_local_credential(tmp_path, _actor()) == legacy
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert legacy not in stored and token_digest(legacy) in stored
    assert other in stored  # another identity is untouched until its own install
    credentials = credentials_from_environment(path.read_text(encoding="utf-8"))
    assert resolve_actor(credentials, f"Bearer {legacy}") == _actor()
    assert resolve_actor(credentials, f"Bearer {other}").role is Role.DEVELOPER


def test_rotation_revokes_the_previous_token(tmp_path):
    first = register_local_credential(tmp_path, _actor())
    second = register_local_credential(tmp_path, _actor(), rotate=True)
    assert second and second != first
    credentials = credentials_from_environment(
        (tmp_path / "config/agent-credentials.json").read_text(encoding="utf-8"))
    assert len(credentials) == 1
    assert resolve_actor(credentials, f"Bearer {second}") == _actor()
    with pytest.raises(AuthorizationError):
        resolve_actor(credentials, f"Bearer {first}")


def test_the_provider_sees_one_token_in_both_forms_as_one_credential(tmp_path):
    token = "T" * 64
    path = tmp_path / "agent-credentials.json"
    path.write_text(json.dumps({token_digest(token): {
        "agent_id": "someone-else", "role": "integrator", "project_ids": ["shop"]}}), encoding="utf-8")
    base = (AgentCredential(token, _actor()),)
    with pytest.raises(ValueError, match="Conflicting"):
        credential_provider(base, path)()
    path.write_text(json.dumps({token_digest(token): {
        "agent_id": "local-qa-abcd1234", "role": "qa", "project_ids": ["shop"]}}), encoding="utf-8")
    merged = credential_provider(base, path)()
    assert len(merged) == 1
    assert resolve_actor(merged, f"Bearer {token}") == _actor()


def test_token_env_var_names_are_portable():
    assert token_env_var("local-qa-1a2b3c4d") == "HOH_MCP_TOKEN_LOCAL_QA_1A2B3C4D"
    assert token_env_var("original.dev") == "HOH_MCP_TOKEN_ORIGINAL_DEV"
