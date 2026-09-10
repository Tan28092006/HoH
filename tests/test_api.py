from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from hoh_control_plane.api import create_app
from hoh_control_plane.domain import Actor, Role


def headers(role: str, agent: str = "agent-01") -> dict[str, str]:
    return {"X-HOH-Agent-Id": agent, "X-HOH-Role": role}


def test_health_board_and_security_headers(tmp_path: Path) -> None:
    app = create_app(
        database_path=tmp_path / "api.db", local_mode=True, seed_demo=True
    )

    with TestClient(app) as client:
        health = client.get("/api/v1/health")
        board = client.get("/api/v1/projects/demo/board")
        dispatch = client.get(
            "/api/v1/projects/demo/dispatch", headers=headers("planner", "ui-observer")
        )

    assert health.status_code == 200
    assert health.json()["data"]["status"] == "ok"
    assert board.status_code == 200
    assert board.json()["data"]["project"]["project_id"] == "demo"
    assert dispatch.status_code == 200
    assert dispatch.json()["data"]["queues"][0]["ticket"]["ticket_id"] == "DEV-142"
    assert "default-src 'self'" in board.headers["content-security-policy"]
    assert board.headers["x-content-type-options"] == "nosniff"


def test_board_exposes_the_active_ticket_owner_and_awaiting_role(tmp_path: Path) -> None:
    """The read-only board tells operators who exclusively owns a ticket phase."""
    app = create_app(database_path=tmp_path / "api.db", local_mode=True, seed_demo=True)
    developer_headers = headers("developer", "developer-board-01")

    with TestClient(app) as client:
        discovered = client.get(
            "/api/v1/metadata/discover",
            params={"project_id": "demo"},
            headers=developer_headers,
        ).json()["data"]
        ticket = discovered["features"][0]["tickets"][0]
        claim = client.post(
            f"/api/v1/tickets/{ticket['ticket_id']}/leases",
            headers=developer_headers,
            json={"expected_version": ticket["version"], "lease_seconds": 900},
        ).json()["data"]
        board = client.get("/api/v1/projects/demo/board").json()["data"]

    active_ticket = board["columns"]["DEV_IN_PROGRESS"][0]
    assert active_ticket["owner"] == {
        "state": "active",
        "agent_id": "developer-board-01",
        "role": "developer",
        "lease_expires_at": claim["lease"]["expires_at"],
        "awaiting_role": None,
    }


def test_local_mode_write_requires_valid_role_headers(tmp_path: Path) -> None:
    app = create_app(database_path=tmp_path / "api.db", local_mode=True)
    payload = {
        "project_id": "p1",
        "name": "Project One",
        "description": "Test",
        "default_branch": "main",
    }

    with TestClient(app) as client:
        unauthenticated = client.post("/api/v1/projects", json=payload)
        invalid_role = client.post(
            "/api/v1/projects",
            json=payload,
            headers=headers("root"),
        )
        created = client.post(
            "/api/v1/projects",
            json=payload,
            headers=headers("planner", "planner-01"),
        )

    assert unauthenticated.status_code == 401
    assert invalid_role.status_code == 401
    assert created.status_code == 201


def test_api_rejects_invalid_ticket_input(tmp_path: Path) -> None:
    app = create_app(database_path=tmp_path / "api.db", local_mode=True, seed_demo=True)

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/features/demo-checkout/tickets",
            headers=headers("planner", "planner-01"),
            json={"ticket_id": "", "title": "", "acceptance_criteria": []},
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_ui_is_served(tmp_path: Path) -> None:
    app = create_app(database_path=tmp_path / "api.db", local_mode=True, seed_demo=True)

    with TestClient(app) as client:
        response = client.get("/")
        script = client.get("/static/app.js")

    assert response.status_code == 200
    assert "Harness Control" in response.text
    assert "Engineering board" in response.text
    assert "Agent activity" in response.text
    assert "Dispatch queue" in response.text
    assert "Create ticket" not in response.text
    assert script.status_code == 200
    assert 'method: "POST"' not in script.text
    assert "agent-activity" in script.text
    assert "/dispatch" in script.text
    assert "ticket.owner" in script.text
    assert "Current owner" in script.text
    assert "ticket-owner" in script.text
    assert '["MERGE_READY", "Integration"]' in script.text
    assert '"integrator.merge_candidate"' in script.text
    assert "new URLSearchParams(window.location.search)" in script.text
    assert '["BACKLOG", "Backlog"]' in script.text
    assert "projectPicker.value = state.projectId" in script.text


def test_ui_static_assets_include_a_favicon(tmp_path: Path) -> None:
    app = create_app(database_path=tmp_path / "api.db", local_mode=True, seed_demo=True)

    with TestClient(app) as client:
        page = client.get("/")
        favicon = client.get("/static/favicon.svg")

    assert 'rel="icon"' in page.text
    assert favicon.status_code == 200
    assert favicon.headers["content-type"].startswith("image/svg+xml")


def test_agent_activity_is_read_only_and_requires_an_identity(tmp_path: Path) -> None:
    app = create_app(database_path=tmp_path / "api.db", local_mode=True, seed_demo=True)

    with TestClient(app) as client:
        missing_identity = client.get("/api/v1/projects/demo/agent-activity")
        observed = client.get(
            "/api/v1/projects/demo/agent-activity",
            headers=headers("planner", "ui-observer"),
        )

    assert missing_identity.status_code == 401
    assert observed.status_code == 200
    assert observed.json()["data"]["calls"] == []


def test_production_token_identity_cannot_spoof_planner_role(tmp_path: Path) -> None:
    identities = {
        "developer-secret": Actor(
            "dev-prod-01", Role.DEVELOPER, frozenset({"production-project"})
        ),
        "planner-secret": Actor(
            "planner-prod-01", Role.PLANNER, frozenset({"production-project"})
        ),
    }
    app = create_app(
        database_path=tmp_path / "api.db",
        local_mode=False,
        token_identities=identities,
    )
    payload = {
        "project_id": "production-project",
        "name": "Production",
        "description": "Protected",
        "default_branch": "main",
    }

    with TestClient(app) as client:
        spoofed = client.post(
            "/api/v1/projects",
            json=payload,
            headers={
                "Authorization": "Bearer developer-secret",
                "X-HOH-Agent-Id": "fake-planner",
                "X-HOH-Role": "planner",
            },
        )
        created = client.post(
            "/api/v1/projects",
            json=payload,
            headers={"Authorization": "Bearer planner-secret"},
        )

    assert spoofed.status_code == 403
    assert created.status_code == 201


def test_rest_api_runs_developer_to_qa_rework_flow(tmp_path: Path) -> None:
    app = create_app(database_path=tmp_path / "api.db", local_mode=True, seed_demo=True)
    dev_headers = headers("developer", "dev-api-01")
    qa_headers = headers("qa", "qa-api-01")

    with TestClient(app) as client:
        discovery = client.get(
            "/api/v1/metadata/discover",
            params={"project_id": "demo"},
            headers=dev_headers,
        )
        ticket = discovery.json()["data"]["features"][0]["tickets"][0]
        context = client.get(
            f"/api/v1/tickets/{ticket['ticket_id']}/context", headers=dev_headers
        )
        feature_context = client.get(
            "/api/v1/features/demo-checkout/context", headers=dev_headers
        )
        claim = client.post(
            f"/api/v1/tickets/{ticket['ticket_id']}/leases",
            headers=dev_headers,
            json={"expected_version": ticket["version"], "lease_seconds": 900},
        ).json()["data"]
        progress = client.post(
            f"/api/v1/tickets/{ticket['ticket_id']}/progress",
            headers=dev_headers,
            json={
                "lease_id": claim["lease"]["lease_id"],
                "message": "Baseline recorded",
            },
        )
        candidate = client.post(
            f"/api/v1/tickets/{ticket['ticket_id']}/candidates",
            headers=dev_headers,
            json={
                "lease_id": claim["lease"]["lease_id"],
                "expected_version": claim["ticket"]["version"],
                "commit_sha": "a" * 40,
                "base_commit": "b" * 40,
                "branch_ref": "agent/dev-api-01/DEV-142",
                "developer_claims": ["Retry path repaired"],
            },
        ).json()["data"]
        qa_claim = client.post(
            f"/api/v1/tickets/{ticket['ticket_id']}/leases",
            headers=qa_headers,
            json={
                "expected_version": candidate["ticket"]["version"],
                "lease_seconds": 900,
            },
        ).json()["data"]
        verdict = client.post(
            f"/api/v1/tickets/{ticket['ticket_id']}/qa-verdict",
            headers=qa_headers,
            json={
                "lease_id": qa_claim["lease"]["lease_id"],
                "candidate_id": candidate["candidate"]["candidate_id"],
                "expected_version": qa_claim["ticket"]["version"],
                "verdict": "failed",
                "findings": [
                    {
                        "criterion": ticket["acceptance_criteria"][0],
                        "status": "failed",
                        "severity": "high",
                        "observation": "Retry control remained disabled",
                        "evidence": [
                            {
                                "type": "test_result",
                                "observation": "Scenario failed",
                                "storage_ref": "evidence://api/retry.json",
                            }
                        ],
                    }
                ],
            },
        )
        events = client.get(
            "/api/v1/projects/demo/events", headers=qa_headers
        )

    assert context.status_code == 200
    assert feature_context.status_code == 200
    assert progress.status_code == 201
    assert verdict.status_code == 201
    assert verdict.json()["data"]["ticket"]["status"] == "REWORK"
    assert events.json()["data"]["events"]
