from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from hoh_control_plane import planner_wakeup
from hoh_control_plane.domain import Actor, AuthorizationError, Role
from hoh_control_plane.mcp_server import create_mcp_server
from hoh_control_plane.planner_wakeup import (
    EndpointNotAllowed,
    SigningSecretMissing,
    build_envelope,
    deliver_pending,
    validate_endpoint,
)


def _interventions(service, project_id: str = "project-1", **kwargs) -> list[dict[str, object]]:
    planner = Actor("planner-01", Role.PLANNER, frozenset({project_id}))
    return service.list_planner_interventions(planner, project_id, **kwargs)


def _blocked(service, actors, seeded, reason: str = "The upstream contract is undecided."):
    ticket = seeded["ticket"]
    claim = service.claim_ticket(
        actors["developer"], ticket["ticket_id"], ticket["version"], 900
    )
    return service.report_blocker(
        actors["developer"],
        ticket["ticket_id"],
        claim["lease"]["lease_id"],
        claim["ticket"]["version"],
        reason,
    )


def _qa_failed(service, actors, seeded, status: str = "failed"):
    ticket = seeded["ticket"]
    claim = service.claim_ticket(
        actors["developer"], ticket["ticket_id"], ticket["version"], 900
    )
    candidate = service.submit_candidate(
        actors["developer"], ticket["ticket_id"], claim["lease"]["lease_id"],
        claim["ticket"]["version"], "a" * 40, "b" * 40, "agent/dev-01/DEV-142", ["Ready."],
        context_snapshot_id=service.get_ticket_context(actors["developer"], ticket["ticket_id"])["context_snapshot_id"],
    )
    qa_claim = service.claim_qa(
        actors["qa"], ticket["ticket_id"], candidate["ticket"]["version"], 900
    )
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
                "status": status,
                "severity": "high",
                "observation": "Secret token abc123 and a raw log line appear here.",
                "evidence": [
                    {
                        "type": "test_result",
                        "observation": "duplicate-order regression reproduced",
                        "storage_ref": "evidence://qa/duplicate-order.json",
                    }
                ],
            }
        ],
        context_snapshot_id=service.get_ticket_context(actors["qa"], ticket["ticket_id"])["context_snapshot_id"],
    )


def test_a_developer_blocker_raises_one_high_severity_request(service, actors, seeded) -> None:
    blocked = _blocked(service, actors, seeded)

    requests = _interventions(service)

    assert len(requests) == 1
    assert requests[0]["event_class"] == "developer_blocker"
    assert requests[0]["severity"] == "high"
    assert requests[0]["status"] == "PENDING"
    assert requests[0]["planner_job_id"] == blocked["planner_job"]["planner_job_id"]
    assert requests[0]["ticket_id"] == seeded["ticket"]["ticket_id"]


def test_a_qa_failure_raises_a_request_bound_to_the_frozen_candidate(
    service, actors, seeded
) -> None:
    failed = _qa_failed(service, actors, seeded)

    requests = _interventions(service)

    assert len(requests) == 1
    assert requests[0]["event_class"] == "qa_failure"
    assert requests[0]["severity"] == "high"
    assert requests[0]["artifact_commit"] == failed["candidate"]["commit_sha"]
    assert requests[0]["candidate_id"] == failed["candidate"]["candidate_id"]


def test_insufficient_evidence_is_raised_at_the_lower_severity(
    service, actors, seeded
) -> None:
    _qa_failed(service, actors, seeded, status="insufficient_evidence")

    requests = _interventions(service)

    assert [request["severity"] for request in requests] == ["normal"]


def test_the_payload_carries_references_only(service, actors, seeded) -> None:
    """No raw claim, observation, log, secret or lease id may leave the plane."""
    _qa_failed(service, actors, seeded)

    payload = service.intervention_payload(_interventions(service)[0])
    serialized = json.dumps(payload)

    assert set(payload) == {
        "intervention_id",
        "project_id",
        "ticket_id",
        "context_ref",
        "planner_job_id",
        "event_class",
        "severity",
        "artifact_commit",
        "candidate_id",
        "evidence_refs",
    }
    assert "abc123" not in serialized
    assert "raw log line" not in serialized
    assert "lease_" not in serialized
    assert all(
        reference.split(":", 1)[0] in {"candidate", "context", "finding", "merge_job"}
        for reference in payload["evidence_refs"]
    )


def test_the_same_unresolved_blocker_does_not_wake_a_planner_twice(
    service, actors, seeded
) -> None:
    blocked = _blocked(service, actors, seeded)
    first = _interventions(service)[0]
    ready = service.set_ticket_ready(
        actors["planner"], seeded["ticket"]["ticket_id"], blocked["ticket"]["version"]
    )
    claim = service.claim_ticket(
        actors["developer"], seeded["ticket"]["ticket_id"], ready["ticket"]["version"], 900
    )

    service.report_blocker(
        actors["developer"],
        seeded["ticket"]["ticket_id"],
        claim["lease"]["lease_id"],
        ready["ticket"]["version"] + 1,
        "The upstream contract is undecided.",
    )
    requests = _interventions(service)

    assert len(requests) == 1
    assert requests[0]["intervention_id"] == first["intervention_id"]
    assert requests[0]["delivery_attempts"] == 0


def test_materially_new_evidence_reopens_delivery_of_the_same_request(
    service, actors, seeded, tmp_path: Path, monkeypatch
) -> None:
    blocked = _blocked(service, actors, seeded)
    monkeypatch.delenv(planner_wakeup.ENDPOINT_VARIABLE, raising=False)
    deliver_pending(service)
    delivered = _interventions(service)[0]
    ready = service.set_ticket_ready(
        actors["planner"], seeded["ticket"]["ticket_id"], blocked["ticket"]["version"]
    )
    claim = service.claim_ticket(
        actors["developer"], seeded["ticket"]["ticket_id"], ready["ticket"]["version"], 900
    )

    service.report_blocker(
        actors["developer"],
        seeded["ticket"]["ticket_id"],
        claim["lease"]["lease_id"],
        ready["ticket"]["version"] + 1,
        "Now also blocked on a second, different contract question.",
    )
    reopened = _interventions(service)[0]

    assert delivered["status"] == "DELIVERED"
    assert reopened["intervention_id"] == delivered["intervention_id"]
    assert reopened["status"] == "PENDING"
    assert len(reopened["evidence_refs"]) == 2


def test_an_offline_endpoint_leaves_the_request_pending_for_the_heartbeat(
    service, actors, seeded, monkeypatch
) -> None:
    _blocked(service, actors, seeded)
    monkeypatch.setenv(planner_wakeup.ENDPOINT_VARIABLE, "http://127.0.0.1:9/offline")

    result = deliver_pending(service)
    pending = _interventions(service)[0]

    assert result["delivered"] == []
    assert len(result["failed"]) == 1
    assert pending["status"] == "PENDING"
    assert pending["delivery_attempts"] == 1
    assert pending["last_error"]
    # The planning problem itself is untouched and still discoverable by polling.
    assert service.discover_planner_jobs(actors["planner"], "project-1")[0]["status"] == "READY"


def test_retrying_delivery_never_duplicates_the_request_or_the_planner_job(
    service, actors, seeded, monkeypatch
) -> None:
    _blocked(service, actors, seeded)
    monkeypatch.setenv(planner_wakeup.ENDPOINT_VARIABLE, "http://127.0.0.1:9/offline")
    deliver_pending(service)
    deliver_pending(service)
    monkeypatch.delenv(planner_wakeup.ENDPOINT_VARIABLE, raising=False)

    deliver_pending(service)
    deliver_pending(service)
    requests = _interventions(service)

    assert len(requests) == 1
    assert requests[0]["status"] == "DELIVERED"
    assert requests[0]["delivery_attempts"] == 3
    assert len(service.discover_planner_jobs(actors["planner"], "project-1")) == 1
    with service.store.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM planner_job_leases"
        ).fetchone()[0] == 0


def test_delivery_without_an_endpoint_writes_a_bounded_local_notification(
    service, actors, seeded, monkeypatch
) -> None:
    _blocked(service, actors, seeded)
    monkeypatch.delenv(planner_wakeup.ENDPOINT_VARIABLE, raising=False)
    monkeypatch.setenv(planner_wakeup.SECRET_VARIABLE, "local-secret")

    result = deliver_pending(service)
    notifications = list((service.store.path.parent / "notifications").glob("*.json"))
    envelope = json.loads(notifications[0].read_text(encoding="utf-8"))

    assert result["endpoint_configured"] is False
    assert len(notifications) == 1
    assert envelope["kind"] == "planner.intervention"
    assert envelope["signature"]
    assert envelope["payload"]["event_class"] == "developer_blocker"
    assert "observation" not in json.dumps(envelope)


def test_a_server_backed_store_writes_notifications_under_its_artifact_root(
    service, actors, seeded, monkeypatch, tmp_path
) -> None:
    """A PostgreSQL store's ``path`` is its DSN string, which has no parent."""
    _blocked(service, actors, seeded)
    monkeypatch.delenv(planner_wakeup.ENDPOINT_VARIABLE, raising=False)
    monkeypatch.setenv(planner_wakeup.SECRET_VARIABLE, "local-secret")
    artifacts = tmp_path / "server-artifacts"

    class ServerBackedStore:
        path = "postgresql://hoh@db.example.invalid/hoh"
        artifact_root = artifacts

        def __getattr__(self, name):
            return getattr(store, name)

    store = service.store
    monkeypatch.setattr(service, "store", ServerBackedStore())

    result = deliver_pending(service)

    assert len(result["delivered"]) == 1
    assert len(list((artifacts / "notifications").glob("*.json"))) == 1


def test_a_registered_endpoint_is_never_sent_an_unsigned_envelope(
    service, actors, seeded, monkeypatch
) -> None:
    """An endpoint without its secret must fail closed, not deliver unauthenticated."""
    _blocked(service, actors, seeded)
    monkeypatch.setenv(planner_wakeup.ENDPOINT_VARIABLE, "http://127.0.0.1:8765/wake")
    monkeypatch.delenv(planner_wakeup.SECRET_VARIABLE, raising=False)
    monkeypatch.setattr(
        planner_wakeup,
        "_open_endpoint",
        lambda *_args, **_kwargs: pytest.fail(
            "An unauthenticated envelope must never reach a registered endpoint"
        ),
    )

    result = deliver_pending(service)
    pending = _interventions(service)[0]

    assert result["delivered"] == []
    assert len(result["failed"]) == 1
    assert pending["status"] == "PENDING"
    assert planner_wakeup.SECRET_VARIABLE in pending["last_error"]
    # The planning problem is untouched and still reachable by polling.
    assert service.discover_planner_jobs(actors["planner"], "project-1")[0]["status"] == "READY"


def test_building_an_endpoint_envelope_without_a_secret_is_refused() -> None:
    with pytest.raises(SigningSecretMissing):
        build_envelope({"intervention_id": "wake_1"}, "nonce-1", None, require_signature=True)
    with pytest.raises(SigningSecretMissing):
        build_envelope({"intervention_id": "wake_1"}, "nonce-1", "", require_signature=True)


def test_a_registered_endpoint_receives_a_signed_envelope(
    service, actors, seeded, monkeypatch
) -> None:
    _blocked(service, actors, seeded)
    monkeypatch.setenv(planner_wakeup.ENDPOINT_VARIABLE, "http://127.0.0.1:8765/wake")
    monkeypatch.setenv(planner_wakeup.SECRET_VARIABLE, "shared-secret")
    sent: list[dict[str, object]] = []

    class _Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def capture(request, *_args, **_kwargs):
        sent.append(json.loads(request.data.decode("utf-8")))
        return _Response()

    monkeypatch.setattr(planner_wakeup, "_open_endpoint", capture)

    result = deliver_pending(service)

    assert len(result["delivered"]) == 1
    assert sent[0]["signature"]
    assert sent[0]["nonce"]
    assert _interventions(service)[0]["status"] == "DELIVERED"


def test_an_allowlisted_endpoint_cannot_redirect_delivery_to_another_host(
    service, actors, seeded, monkeypatch
) -> None:
    """Validating the configured URL is not enough if urllib then follows a 302.

    A real loopback server answers with a redirect to a non-loopback host. The
    adapter must refuse the hop rather than resolve or contact that target.
    """
    _blocked(service, actors, seeded)

    class _Redirector(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):  # noqa: N802
            # Drain the request body before answering. Replying to an unread
            # request lets Windows reset the connection, which would surface as a
            # socket error instead of the redirect this test is about.
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self.send_response(302)
            self.send_header("Location", "http://outside.invalid/next")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *_args):  # keep the test output quiet
            return

    server = HTTPServer(("127.0.0.1", 0), _Redirector)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    resolved: list[str] = []
    real_getaddrinfo = socket.getaddrinfo

    def watched_getaddrinfo(host, *args, **kwargs):
        resolved.append(host)
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", watched_getaddrinfo)
    monkeypatch.setenv(
        planner_wakeup.ENDPOINT_VARIABLE, f"http://127.0.0.1:{server.server_port}/wake"
    )
    monkeypatch.setenv(planner_wakeup.SECRET_VARIABLE, "shared-secret")

    try:
        result = deliver_pending(service)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    pending = _interventions(service)[0]

    assert result["delivered"] == []
    assert len(result["failed"]) == 1
    assert "redirect" in pending["last_error"].lower()
    assert pending["status"] == "PENDING"
    assert "outside.invalid" not in resolved
    # The planning problem survives the refused delivery.
    assert service.discover_planner_jobs(actors["planner"], "project-1")[0]["status"] == "READY"


def test_the_envelope_is_signed_and_replay_identifiable() -> None:
    payload = {"intervention_id": "wake_1", "ticket_id": "DEV-1"}

    signed = build_envelope(payload, "nonce-1", "shared-secret")
    tampered = build_envelope({**payload, "ticket_id": "DEV-2"}, "nonce-1", "shared-secret")
    unsigned = build_envelope(payload, "nonce-1", None)

    assert signed["nonce"] == "nonce-1"
    assert signed["issued_at"].endswith("Z")
    assert signed["signature"] != tampered["signature"]
    assert "signature" not in unsigned


@pytest.mark.parametrize(
    "endpoint",
    ["http://example.com/wake", "https://10.0.0.5/wake", "file:///tmp/wake", "ftp://127.0.0.1"],
)
def test_a_non_loopback_endpoint_is_refused(endpoint: str) -> None:
    with pytest.raises(EndpointNotAllowed):
        validate_endpoint(endpoint)


def test_a_disallowed_endpoint_fails_delivery_instead_of_calling_it(
    service, actors, seeded, monkeypatch
) -> None:
    _blocked(service, actors, seeded)
    monkeypatch.setenv(planner_wakeup.ENDPOINT_VARIABLE, "http://example.com/wake")
    monkeypatch.setattr(
        planner_wakeup,
        "_open_endpoint",
        lambda *_args, **_kwargs: pytest.fail("A disallowed endpoint must never be called"),
    )

    result = deliver_pending(service)

    assert len(result["failed"]) == 1
    assert _interventions(service)[0]["status"] == "PENDING"


def test_only_a_planner_may_acknowledge_and_it_resolves_nothing(
    service, actors, seeded
) -> None:
    _blocked(service, actors, seeded)
    intervention = _interventions(service)[0]

    for role_key in ("developer", "qa", "integrator"):
        with pytest.raises(AuthorizationError):
            service.acknowledge_planner_intervention(
                actors[role_key], intervention["intervention_id"]
            )
    acknowledged = service.acknowledge_planner_intervention(
        actors["planner"], intervention["intervention_id"]
    )

    assert acknowledged["status"] == "ACKNOWLEDGED"
    assert acknowledged["acknowledged_by"] == actors["planner"].agent_id
    # Acknowledging receipt must not close the decision or take the ticket.
    assert service.discover_planner_jobs(actors["planner"], "project-1")[0]["status"] == "READY"
    assert service.get_ticket_context(
        actors["planner"], seeded["ticket"]["ticket_id"]
    )["ticket"]["status"] == "BLOCKED"


def test_the_read_only_dashboard_exposes_pending_and_delivered_requests(
    service, actors, seeded, monkeypatch
) -> None:
    _blocked(service, actors, seeded)
    monkeypatch.delenv(planner_wakeup.ENDPOINT_VARIABLE, raising=False)

    pending_view = service.dispatch_overview(actors["planner"], "project-1")
    deliver_pending(service)
    delivered_view = service.dispatch_overview(actors["planner"], "project-1")

    assert pending_view["planner_interventions"][0]["status"] == "PENDING"
    assert delivered_view["planner_interventions"][0]["status"] == "DELIVERED"
    assert "observation" not in json.dumps(delivered_view["planner_interventions"])


@pytest.mark.anyio
async def test_planner_mcp_reads_and_acknowledges_a_request(service, actors, seeded) -> None:
    _blocked(service, actors, seeded)
    server = create_mcp_server(service, actors["planner"])

    names = {tool.name for tool in await server.list_tools()}
    _, listed = await server.call_tool(
        "planner.list_interventions", {"project_id": "project-1"}
    )
    _, acknowledged = await server.call_tool(
        "planner.acknowledge_intervention",
        {"intervention_id": listed["interventions"][0]["intervention_id"]},
    )

    assert {"planner.list_interventions", "planner.acknowledge_intervention"}.issubset(names)
    assert listed["interventions"][0]["status"] == "PENDING"
    assert acknowledged["intervention"]["status"] == "ACKNOWLEDGED"
    developer_names = {
        tool.name for tool in await create_mcp_server(service, actors["developer"]).list_tools()
    }
    assert "planner.acknowledge_intervention" not in developer_names
