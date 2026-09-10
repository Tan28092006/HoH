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
