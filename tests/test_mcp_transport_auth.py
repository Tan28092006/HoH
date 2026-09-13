"""A network-served MCP endpoint authenticates before it dispatches (HOH-243)."""
from __future__ import annotations

import json

import pytest

from hoh_control_plane.auth import (
    STDIO_TRANSPORT,
    AgentCredential,
    credentials_from_environment,
    requested_transport,
    resolve_actor,
)
from hoh_control_plane.domain import Actor, AuthorizationError, Role, ValidationError
from hoh_control_plane.mcp_server import create_mcp_http_app


DEV_TOKEN = "dev-token-that-is-long-enough-to-be-a-secret"
INTEGRATOR_TOKEN = "integrator-token-that-is-long-enough-too"


def _credentials() -> tuple[AgentCredential, ...]:
    return (
        AgentCredential(
            DEV_TOKEN, Actor("dev-01", Role.DEVELOPER, frozenset({"project-1"}))
        ),
        AgentCredential(
            INTEGRATOR_TOKEN,
            Actor("integrator-01", Role.INTEGRATOR, frozenset({"project-1"})),
        ),
    )


def _tokens_json() -> str:
    return json.dumps(
        {
            DEV_TOKEN: {
                "agent_id": "dev-01",
                "role": "developer",
                "project_ids": ["project-1"],
            }
        }
    )


class _Recorder:
    """Collects an ASGI response without running a real server."""

    def __init__(self) -> None:
        self.status: int | None = None
        self.body = b""
        self.headers: list[tuple[bytes, bytes]] = []

    async def send(self, message: dict) -> None:
        if message["type"] == "http.response.start":
            self.status = message["status"]
            self.headers = message.get("headers", [])
        elif message["type"] == "http.response.body":
            self.body += message.get("body", b"")

    async def receive(self) -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}


def _scope(authorization: str | None = None) -> dict:
    headers: list[tuple[bytes, bytes]] = []
    if authorization is not None:
        headers.append((b"Authorization", authorization.encode("latin-1")))
    return {"type": "http", "method": "POST", "path": "/mcp", "headers": headers}


# --- credential parsing -------------------------------------------------------


def test_configured_credentials_are_read_as_server_side_identity() -> None:
    credentials = credentials_from_environment(_tokens_json())

    assert len(credentials) == 1
    assert credentials[0].actor.role is Role.DEVELOPER
    assert credentials[0].actor.project_ids == frozenset({"project-1"})


def test_no_configuration_yields_no_credentials() -> None:
    assert credentials_from_environment("") == ()


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "[]",
        "{}",
        json.dumps({"short": {"agent_id": "a", "role": "developer"}}),
        json.dumps({DEV_TOKEN: {"agent_id": "a", "role": "wizard"}}),
        json.dumps({DEV_TOKEN: {"role": "developer"}}),
        json.dumps({DEV_TOKEN: {"agent_id": "a", "role": "developer", "project_ids": "p"}}),
    ],
)
def test_malformed_credential_configuration_is_refused(raw: str) -> None:
    with pytest.raises(ValidationError):
        credentials_from_environment(raw)


# --- resolution ---------------------------------------------------------------


def test_a_valid_credential_resolves_to_its_configured_identity() -> None:
    actor = resolve_actor(_credentials(), f"Bearer {INTEGRATOR_TOKEN}")

    assert actor.agent_id == "integrator-01"
    assert actor.role is Role.INTEGRATOR


@pytest.mark.parametrize(
    "authorization",
    [None, "", "Bearer ", "Bearer wrong-token", DEV_TOKEN, f"Basic {DEV_TOKEN}"],
)
def test_a_missing_or_wrong_credential_is_refused(authorization: str | None) -> None:
    with pytest.raises(AuthorizationError):
        resolve_actor(_credentials(), authorization)


@pytest.mark.parametrize("position", [0, 1])
def test_a_credential_resolves_the_same_wherever_it_sits_in_the_list(
    position: int,
) -> None:
    """Order must not change the answer; comparison runs over the whole list."""
    credentials = _credentials()
    ordered = credentials if position == 0 else tuple(reversed(credentials))

    actor = resolve_actor(ordered, f"Bearer {INTEGRATOR_TOKEN}")

    assert actor.agent_id == "integrator-01"


def test_a_token_that_is_a_prefix_of_a_real_one_is_refused() -> None:
    """A length-equality shortcut would leak how much of a guess was right."""
    with pytest.raises(AuthorizationError):
        resolve_actor(_credentials(), f"Bearer {DEV_TOKEN[:-1]}")
    with pytest.raises(AuthorizationError):
        resolve_actor(_credentials(), f"Bearer {DEV_TOKEN}x")


# --- transport selection ------------------------------------------------------


def test_the_default_transport_is_stdio() -> None:
    """Listening on a network has to be chosen, never fallen into."""
    assert requested_transport("") == STDIO_TRANSPORT
    assert requested_transport("   ") == STDIO_TRANSPORT


@pytest.mark.parametrize("transport", ["streamable-http", "sse", "STDIO"])
def test_known_transports_are_accepted(transport: str) -> None:
    assert requested_transport(transport) == transport.strip().lower()


def test_an_unknown_transport_is_refused() -> None:
    with pytest.raises(ValidationError):
        requested_transport("carrier-pigeon")


# --- the served endpoint ------------------------------------------------------


def test_a_network_endpoint_without_credentials_cannot_be_built(service) -> None:
    """Serving anonymously is the failure this whole ticket exists to prevent."""
    with pytest.raises(ValidationError):
        create_mcp_http_app(service, ())


@pytest.mark.anyio
@pytest.mark.parametrize("authorization", [None, "Bearer wrong-token", "garbage"])
async def test_an_unauthenticated_request_is_refused_before_any_tool_runs(
    service, authorization: str | None
) -> None:
    app = create_mcp_http_app(service, _credentials())
    recorder = _Recorder()

    await app(_scope(authorization), recorder.receive, recorder.send)

    assert recorder.status == 401
    assert b"unauthorized" in recorder.body
    assert (b"www-authenticate", b"Bearer") in recorder.headers
    # The refusal must not reveal which credentials exist.
    assert DEV_TOKEN.encode() not in recorder.body
    assert INTEGRATOR_TOKEN.encode() not in recorder.body


@pytest.mark.anyio
async def test_each_credential_reaches_only_its_own_role_surface(service) -> None:
    """A role's tools are absent from another role's endpoint, not merely refused."""
    from hoh_control_plane.mcp_server import create_mcp_server

    developer, integrator = _credentials()
    developer_tools = {
        tool.name for tool in await create_mcp_server(service, developer.actor).list_tools()
    }
    integrator_tools = {
        tool.name for tool in await create_mcp_server(service, integrator.actor).list_tools()
    }

    assert "integrator.merge_candidate" in integrator_tools
    assert "integrator.merge_candidate" not in developer_tools
    assert "dev.submit_candidate" in developer_tools
    assert "dev.submit_candidate" not in integrator_tools


def test_a_client_supplied_role_cannot_override_the_credential(service) -> None:
    """Only the configured mapping decides a role; a header claim is not input."""
    credentials = _credentials()

    actor = resolve_actor(credentials, f"Bearer {DEV_TOKEN}")

    assert actor.role is Role.DEVELOPER
    assert actor.agent_id == "dev-01"


@pytest.mark.parametrize("launcher", ["_start_qa_test_worker", "_start_worktree_provision_worker", "_start_candidate_submission_worker", "_start_merge_worker"])
def test_postgres_worker_credentials_are_not_in_process_arguments(launcher):
    from unittest.mock import patch
    from hoh_control_plane import mcp_server
    dsn = "postgresql://fixture-user:fixture-password@db.invalid/hoh"
    with patch.object(mcp_server.subprocess, "Popen") as popen:
        getattr(mcp_server, launcher)(dsn, "fixture-job")
    assert all("fixture-password" not in arg for arg in popen.call_args.args[0])
    assert popen.call_args.kwargs["env"]["HOH_DATABASE_URL"] == dsn


@pytest.mark.parametrize("header", ["Bearer café", "Bearer 🔑"])
def test_non_ascii_credentials_are_rejected_without_a_server_error(header) -> None:
    with pytest.raises(AuthorizationError):
        resolve_actor(_credentials(), header)


def test_http_client_can_initialize_list_and_call_tools(service) -> None:
    """Exercise the real SDK transport and its lifespan, not only the auth wrapper."""
    from starlette.testclient import TestClient

    app = create_mcp_http_app(service, _credentials())
    headers = {
        "Authorization": f"Bearer {DEV_TOKEN}",
        "Accept": "application/json, text/event-stream",
    }

    def result(response):
        assert response.status_code == 200, response.text
        if response.headers["content-type"].startswith("text/event-stream"):
            return json.loads(next(line[6:] for line in response.text.splitlines()
                                   if line.startswith("data: ")))
        return response.json()

    with TestClient(app, base_url="http://127.0.0.1") as client:
        initialized = client.post("/mcp", headers=headers, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                       "clientInfo": {"name": "fresh-session", "version": "1"}},
        })
        assert "serverInfo" in result(initialized)["result"]
        if "mcp-session-id" in initialized.headers:
            headers["Mcp-Session-Id"] = initialized.headers["mcp-session-id"]
        client.post("/mcp", headers=headers, json={
            "jsonrpc": "2.0", "method": "notifications/initialized",
        })
        listed = result(client.post("/mcp", headers=headers, json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/list",
        }))
        names = {tool["name"] for tool in listed["result"]["tools"]}
        assert "dev.submit_candidate" in names
        assert "integrator.merge_candidate" not in names
        called = result(client.post("/mcp", headers=headers, json={
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "identity.get_capabilities", "arguments": {}},
        }))
        assert called["result"]["structuredContent"]["agent_id"] == "dev-01"
        assert "mcp-session-id" not in initialized.headers, "HTTP sessions must not accumulate between chats"


def test_shared_host_loads_new_profiles_and_revokes_without_restart(service, tmp_path):
    from starlette.testclient import TestClient
    from hoh_control_plane.local_credentials import credential_provider, register_local_credential
    actor = Actor('new-qa', Role.QA, frozenset({'project-1'}))
    path = tmp_path / 'config/agent-credentials.json'
    app = create_mcp_http_app(service, _credentials(), credentials_provider=credential_provider(_credentials(), path))
    with TestClient(app, base_url='http://127.0.0.1') as client:
        token = register_local_credential(tmp_path, actor)
        assert register_local_credential(tmp_path, actor) == token
        headers = {'Authorization': f'Bearer {token}', 'Accept': 'application/json, text/event-stream'}
        payload = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}
        response = client.post('/mcp', headers=headers, json=payload)
        assert response.status_code == 200
        data = json.loads(next(line[6:] for line in response.text.splitlines() if line.startswith('data: ')))
        names = {tool['name'] for tool in data['result']['tools']}
        assert 'qa.run_project_tests' in names and 'dev.submit_candidate' not in names
        # Revocation is checked even for an actor whose server is already cached.
        path.unlink()
        assert client.post('/mcp', headers=headers, json=payload).status_code == 401
        path.write_text('invalid json')
        rejected = client.post('/mcp', headers=headers, json=payload)
        assert rejected.status_code == 401 and token not in rejected.text


def test_shared_http_reconnect_fences_old_wait_and_keeps_same_actor(service, actors):
    from concurrent.futures import ThreadPoolExecutor
    import time
    from starlette.testclient import TestClient
    service.create_project(actors['planner'], project_id='project-1', name='Wait recovery', description='', default_branch='main')
    service.register_agent(actors['developer'], skills=['developer'], capacity=1)
    headers = {'Authorization': f'Bearer {DEV_TOKEN}', 'Accept': 'application/json, text/event-stream'}
    app = create_mcp_http_app(service, _credentials())
    with TestClient(app, base_url='http://127.0.0.1') as client, ThreadPoolExecutor(2) as pool:
        def call(identifier, arguments):
            response = client.post('/mcp', headers=headers, json={
                'jsonrpc':'2.0', 'id':identifier, 'method':'tools/call',
                'params':{'name':'work.wait_for_event', 'arguments':arguments}})
            assert response.status_code == 200, response.text
            messages = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
            result = next(message['result'] for message in messages if message.get('id') == identifier)
            assert not result.get('isError'), result
            return result['structuredContent']
        previous = pool.submit(call, 1, {'project_id':'project-1', 'timeout_seconds':5})
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            wait = service.session_bootstrap(actors['developer'], 'project-1')['event_wait']
            if wait is not None:
                break
            time.sleep(.02)
        assert wait is not None
        resumed = call(2, {'project_id':'project-1', 'timeout_seconds':1, 'resume_wait_id':wait['wait_id']})
        assert previous.result(timeout=3)['status'] == 'superseded'
        assert resumed['status'] == 'timeout'
