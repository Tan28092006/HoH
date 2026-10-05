from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from mcp.server.fastmcp.exceptions import ToolError

from hoh_control_plane.api import create_app
from hoh_control_plane.domain import Actor, AuthorizationError, ConflictError, NotFoundError, Role
from hoh_control_plane.mcp_server import create_mcp_server


def _rest_app(tmp_path: Path):
    """One REST app seeded with the same demo project the MCP actors work against."""
    return create_app(database_path=tmp_path / "parity.db", local_mode=True, seed_demo=True)


def _headers(role: str, agent: str = "parity-01") -> dict[str, str]:
    return {"X-HOH-Agent-Id": agent, "X-HOH-Role": role}


async def _mcp_error(service, actor: Actor, tool: str, arguments: dict[str, object]) -> str:
    with pytest.raises(ToolError) as excinfo:
        await create_mcp_server(service, actor).call_tool(tool, arguments)
    return str(excinfo.value)


@pytest.mark.anyio
async def test_stale_version_reports_the_same_code_through_mcp_and_rest(
    service, actors, seeded, tmp_path: Path
) -> None:
    ticket = seeded["ticket"]
    mcp_message = await _mcp_error(
        service,
        actors["developer"],
        "work.claim",
        {"ticket_id": ticket["ticket_id"], "expected_version": ticket["version"] + 7},
    )

    with TestClient(_rest_app(tmp_path)) as client:
        discovered = client.get(
            "/api/v1/metadata/discover",
            params={"project_id": "demo"},
            headers=_headers("developer"),
        ).json()["data"]
        rest_ticket = discovered["features"][0]["tickets"][0]
        rest = client.post(
            f"/api/v1/tickets/{rest_ticket['ticket_id']}/leases",
            headers=_headers("developer"),
            json={"expected_version": rest_ticket["version"] + 7},
        )

    assert ConflictError.code in mcp_message
    assert rest.status_code == ConflictError.status_code
    assert rest.json()["error"]["code"] == ConflictError.code


@pytest.mark.anyio
async def test_unauthorized_role_reports_the_same_code_through_mcp_and_rest(
    service, actors, seeded, tmp_path: Path
) -> None:
    ticket = seeded["ticket"]
    mcp_message = await _mcp_error(
        service,
        actors["qa"],
        "qa.claim_candidate",
        {"ticket_id": ticket["ticket_id"], "expected_version": ticket["version"]},
    )

    with TestClient(_rest_app(tmp_path)) as client:
        rest = client.post(
            "/api/v1/projects",
            headers=_headers("developer"),
            json={"project_id": "p9", "name": "Nope", "description": "", "default_branch": "main"},
        )

    assert ConflictError.code in mcp_message or AuthorizationError.code in mcp_message
    assert rest.status_code == AuthorizationError.status_code
    assert rest.json()["error"]["code"] == AuthorizationError.code


@pytest.mark.anyio
async def test_unknown_resource_reports_the_same_code_through_mcp_and_rest(
    service, actors, seeded, tmp_path: Path
) -> None:
    mcp_message = await _mcp_error(
        service, actors["developer"], "ticket.get_context", {"ticket_ref": "ticket:MISSING-1"}
    )

    with TestClient(_rest_app(tmp_path)) as client:
        rest = client.get(
            "/api/v1/tickets/MISSING-1/context", headers=_headers("developer")
        )

    assert NotFoundError.code in mcp_message
    assert rest.status_code == NotFoundError.status_code
    assert rest.json()["error"]["code"] == NotFoundError.code


@pytest.mark.anyio
async def test_a_raw_storage_error_is_normalized_in_both_transports(
    service, actors, seeded, tmp_path: Path, monkeypatch
) -> None:
    """Neither transport may expose SQLite text, a stack trace or transport internals."""

    def raise_integrity_error(*_args, **_kwargs):
        raise sqlite3.IntegrityError("FOREIGN KEY constraint failed")

    monkeypatch.setattr(service, "claim_ticket", raise_integrity_error)
    ticket = seeded["ticket"]
    mcp_message = await _mcp_error(
        service,
        actors["developer"],
        "work.claim",
        {"ticket_id": ticket["ticket_id"], "expected_version": ticket["version"]},
    )

    app = _rest_app(tmp_path)
    monkeypatch.setattr(app.state.service, "claim_ticket", raise_integrity_error)
    with TestClient(app, raise_server_exceptions=False) as client:
        discovered = client.get(
            "/api/v1/metadata/discover",
            params={"project_id": "demo"},
            headers=_headers("developer"),
        ).json()["data"]
        rest_ticket = discovered["features"][0]["tickets"][0]
        rest = client.post(
            f"/api/v1/tickets/{rest_ticket['ticket_id']}/leases",
            headers=_headers("developer"),
            json={"expected_version": rest_ticket["version"]},
        )

    body = rest.json()
    assert "foreign key" not in mcp_message.lower()
    assert "constraint" not in mcp_message.lower()
    assert rest.status_code == ConflictError.status_code
    assert body["error"]["code"] == ConflictError.code
    assert "foreign key" not in rest.text.lower()
    assert "traceback" not in rest.text.lower()


@pytest.mark.anyio
async def test_an_unexpected_internal_error_is_not_leaked_by_rest(
    tmp_path: Path, monkeypatch
) -> None:
    app = _rest_app(tmp_path)

    def explode(*_args, **_kwargs):
        raise RuntimeError("secret-token-abc123 leaked from an internal call")

    monkeypatch.setattr(app.state.service, "create_project", explode)

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(
            "/api/v1/projects",
            headers=_headers("planner"),
            json={"project_id": "p8", "name": "Boom", "description": "", "default_branch": "main"},
        )

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert "secret-token-abc123" not in response.text
    assert "traceback" not in response.text.lower()


@pytest.mark.anyio
async def test_every_mcp_domain_failure_is_prefixed_with_its_stable_code(
    service, actors, seeded
) -> None:
    """A stdio client can branch on the code without parsing prose.

    FastMCP prepends its own "Error executing tool <name>:" framing, so the
    contract is that the bracketed code introduces the domain message inside it.
    """
    message = await _mcp_error(
        service, actors["developer"], "ticket.get_context", {"ticket_ref": "ticket:MISSING-2"}
    )

    assert f"[{NotFoundError.code}] " in message
    assert message.index(f"[{NotFoundError.code}]") < message.index("not found")


def test_the_idempotency_conflict_code_is_identical_in_both_transports(
    service, actors, seeded, tmp_path: Path
) -> None:
    from hoh_control_plane.domain import IdempotencyConflictError

    ticket = seeded["ticket"]
    payload = {
        "ticket_id": ticket["ticket_id"],
        "expected_version": ticket["version"],
        "lease_seconds": 900,
    }
    service.run_idempotent(
        actors["developer"],
        operation="work.claim",
        idempotency_key="parity-key",
        payload=payload,
        run=lambda: service.claim_ticket(actors["developer"], **payload),
    )
    with pytest.raises(IdempotencyConflictError) as excinfo:
        service.run_idempotent(
            actors["developer"],
            operation="work.claim",
            idempotency_key="parity-key",
            payload={**payload, "lease_seconds": 1800},
            run=lambda: pytest.fail("A mismatched replay must not execute"),
        )

    app = _rest_app(tmp_path)
    headers = {**_headers("planner"), "Idempotency-Key": "parity-rest-key"}
    body = {"project_id": "p7", "name": "Seven", "description": "", "default_branch": "main"}
    with TestClient(app) as client:
        client.post("/api/v1/projects", json=body, headers=headers)
        mismatch = client.post(
            "/api/v1/projects", json={**body, "name": "Eight"}, headers=headers
        )

    assert excinfo.value.code == IdempotencyConflictError.code
    assert mismatch.status_code == IdempotencyConflictError.status_code
    assert mismatch.json()["error"]["code"] == IdempotencyConflictError.code


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("details", "hint_fragment"),
    [
        ({"missing_criteria": ["AC-1"], "contract_estimated_tokens": 1300}, None),
        ({"reason": "repository_reconciliation_required"}, "work.wait_for_event"),
        ({"reason": "lease_expired"}, "claim again"),
    ],
)
async def test_mcp_and_rest_carry_the_same_error_details(
    service, actors, seeded, tmp_path: Path, monkeypatch, details, hint_fragment
) -> None:
    """MCP used to drop DomainError.details; both transports now return them."""
    import json

    def refuse(*_args, **_kwargs):
        raise ConflictError("refused for a reason", details)

    monkeypatch.setattr(service, "claim_ticket", refuse)
    ticket = seeded["ticket"]
    mcp_message = await _mcp_error(
        service,
        actors["developer"],
        "work.claim",
        {"ticket_id": ticket["ticket_id"], "expected_version": ticket["version"]},
    )
    mcp_extra = json.loads(mcp_message[mcp_message.index("{"):])

    app = _rest_app(tmp_path)
    monkeypatch.setattr(app.state.service, "claim_ticket", refuse)
    with TestClient(app) as client:
        rest = client.post(
            f"/api/v1/tickets/{ticket['ticket_id']}/leases",
            headers=_headers("developer"),
            json={"expected_version": ticket["version"]},
        )

    assert f"[{ConflictError.code}] refused for a reason " in mcp_message
    assert mcp_extra["details"] == rest.json()["error"]["details"] == details
    if hint_fragment:
        assert hint_fragment in mcp_extra["retry_hint"]
    else:
        assert "retry_hint" not in mcp_extra


@pytest.mark.anyio
async def test_a_stale_context_error_carries_a_retry_hint_through_mcp(
    service, actors, seeded, monkeypatch
) -> None:
    from hoh_control_plane.domain import ContextStaleError

    def stale(*_args, **_kwargs):
        raise ContextStaleError("stale", {"stale": ["ticket"]})

    monkeypatch.setattr(service, "claim_ticket", stale)
    ticket = seeded["ticket"]
    message = await _mcp_error(
        service,
        actors["developer"],
        "work.claim",
        {"ticket_id": ticket["ticket_id"], "expected_version": ticket["version"]},
    )
    assert '"stale":["ticket"]' in message
    assert "ticket.get_context" in message


# ---------------------------------------------------------------------------
# One schema for structured agent writes (review H2). The MCP input schema, the
# REST model and the service limit all come from hoh_control_plane.contracts.


def _resolve(schema: dict, node: dict) -> dict:
    """Follow a $ref and unwrap an Optional so the test reads the real type."""
    while True:
        if "$ref" in node:
            node = schema["$defs"][node["$ref"].rsplit("/", 1)[1]]
            continue
        options = node.get("anyOf")
        if options and any(option.get("type") == "null" for option in options):
            node = next(option for option in options if option.get("type") != "null")
            continue
        return node


async def _input_schema(service, actor: Actor, tool: str) -> dict:
    tools = {item.name: item for item in await create_mcp_server(service, actor).list_tools()}
    return tools[tool].inputSchema


@pytest.mark.anyio
async def test_the_verdict_schema_carries_the_service_enums_and_limits(service, actors) -> None:
    from hoh_control_plane import contracts

    schema = await _input_schema(service, actors["qa"], "qa.submit_verdict")
    properties = schema["properties"]
    assert sorted(_resolve(schema, properties["verdict"])["enum"]) == ["failed", "passed"]
    findings = _resolve(schema, properties["findings"])
    assert findings["maxItems"] == contracts.MAX_FINDINGS
    finding = _resolve(schema, findings["items"])
    assert set(_resolve(schema, finding["properties"]["status"])["enum"]) == contracts.FINDING_STATUSES
    assert finding["properties"]["criterion"]["maxLength"] == contracts.FINDING_CRITERION_CHARS
    assert finding["properties"]["observation"]["maxLength"] == contracts.OBSERVATION_CHARS
    assert finding["additionalProperties"] is False
    evidence = _resolve(schema, _resolve(schema, finding["properties"]["evidence"])["items"])
    assert evidence["properties"]["storage_ref"]["maxLength"] == contracts.STORAGE_REF_CHARS
    assert set(evidence["required"]) == {"type", "observation", "storage_ref"}


@pytest.mark.anyio
async def test_the_candidate_schema_bounds_developer_claims(service, actors) -> None:
    from hoh_control_plane import contracts

    schema = await _input_schema(service, actors["developer"], "dev.submit_candidate")
    claims = _resolve(schema, schema["properties"]["developer_claims"])
    assert claims["maxItems"] == contracts.MAX_DEVELOPER_CLAIMS
    assert claims["items"]["maxLength"] == contracts.DEVELOPER_CLAIM_CHARS


@pytest.mark.anyio
@pytest.mark.parametrize(
    "tool", ["planner.configure_validation_contract", "planner.create_ticket", "planner.update_ticket"]
)
async def test_every_validation_contract_schema_uses_the_service_limits(
    service, actors, tool
) -> None:
    from hoh_control_plane import contracts

    schema = await _input_schema(service, actors["planner"], tool)
    contract = _resolve(schema, schema["properties"]["validation_contract"])
    assert contract["maxItems"] == contracts.CONTRACT_MAX_SCENARIOS
    scenario = _resolve(schema, contract["items"])
    assert scenario["properties"]["criterion"]["maxLength"] == contracts.CONTRACT_TEXT_CHARS
    assert scenario["properties"]["expected_observation"]["maxLength"] == contracts.CONTRACT_TEXT_CHARS
    assert scenario["properties"]["scenario_ref"]["maxLength"] == contracts.SCENARIO_REF_CHARS
    assert set(_resolve(schema, scenario["properties"]["method"])["enum"]) == contracts.VALIDATION_METHODS


def test_rest_and_the_service_share_the_contract_types_and_limits() -> None:
    from hoh_control_plane import api, contracts
    from hoh_control_plane.service import _common

    assert api.FindingInput is contracts.FindingInput
    assert api.EvidenceInput is contracts.EvidenceInput
    assert api.ValidationScenarioInput is contracts.ValidationScenarioInput
    assert api.VerdictRequest.model_fields["verdict"].annotation == contracts.Verdict
    assert _common._CONTRACT_TEXT_CHARS == contracts.CONTRACT_TEXT_CHARS
    assert _common._CONTRACT_TOKEN_BUDGET == contracts.CONTRACT_TOKEN_BUDGET


@pytest.mark.anyio
async def test_an_uppercase_verdict_is_refused_by_the_schema(service, actors, seeded) -> None:
    """PASS/FAIL used to fail only deep inside the service; now the schema says so."""
    with pytest.raises(ToolError) as refused:
        await create_mcp_server(service, actors["qa"]).call_tool(
            "qa.submit_verdict",
            {
                "ticket_id": seeded["ticket"]["ticket_id"], "lease_id": "lease",
                "candidate_id": "cand", "expected_version": 1, "verdict": "PASS",
                "findings": [], "context_snapshot_id": "snapshot",
            },
        )
    assert "verdict" in str(refused.value)


def test_the_service_refuses_what_the_contract_refuses(service, actors) -> None:
    """A contract scenario one character over the limit fails in the service too."""
    from hoh_control_plane import contracts
    from hoh_control_plane.domain import ValidationError

    scenario = {
        "criterion": "c" * (contracts.CONTRACT_TEXT_CHARS + 1), "scenario_ref": "s",
        "method": "black_box", "expected_observation": "o",
        "required_evidence_types": ["test_result"],
    }
    with pytest.raises(ValidationError, match="at most"):
        service._validated_validation_contract([scenario["criterion"]], [scenario])
    with pytest.raises(Exception):
        contracts.ValidationScenarioInput(**scenario)


@pytest.mark.anyio
@pytest.mark.parametrize("role", ["planner", "developer", "qa", "integrator"])
async def test_capabilities_name_only_tools_the_role_actually_has(service, actors, role) -> None:
    """identity.get_capabilities used to advertise work:inbox to Planner."""
    from hoh_control_plane.service.discovery import CAPABILITY_TOOLS

    actor = actors[role]
    tool_names = {item.name for item in await create_mcp_server(service, actor).list_tools()}
    advertised = set(service.capabilities(actor))
    for capability, tool in CAPABILITY_TOOLS.items():
        assert (capability in advertised) == (tool in tool_names), (role, capability, tool)
