"""Build the role-scoped MCP tool surface for one authenticated actor."""
from __future__ import annotations

import os
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from hoh_control_plane.domain import Actor
from hoh_control_plane.service import ControlPlaneService
from hoh_control_plane.mcp.invoke import _CompactFastMCP, make_invoke
from hoh_control_plane.mcp.tools import common, developer, integrator, planner, qa


def create_mcp_server(service: ControlPlaneService, actor: Actor, *, stateless_http: bool = False) -> FastMCP:
    """Create the smallest MCP tool surface permitted for one authenticated role."""
    server = _CompactFastMCP(
        "Harness Control Plane",
        stateless_http=stateless_http,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=["127.0.0.1", "127.0.0.1:*", "localhost", "localhost:*", "[::1]", "[::1]:*"]
            + [value.strip() for value in os.getenv("HOH_MCP_ALLOWED_HOSTS", "").split(",") if value.strip()],
            allowed_origins=["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"]
            + [value.strip() for value in os.getenv("HOH_MCP_ALLOWED_ORIGINS", "").split(",") if value.strip()],
        ),
        instructions=(
            "Discover work first, resolve typed context references, and only invoke "
            "role-authorized write tools. Register presence; call work.wait_for_event "
            "to wait without model polling. A ticket lease remains the exclusive claim. "
            "Every MCP invocation is audit logged with redacted arguments."
        ),
    )
    invoke = make_invoke(service, actor)
    # Registration order is the order clients list the tools in.
    common.register(server, service, actor, invoke)
    planner.register_lesson_audit(server, service, actor, invoke)
    common.register_inbox(server, service, actor, invoke)
    planner.register(server, service, actor, invoke)
    developer.register(server, service, actor, invoke)
    common.register_lease_heartbeat(server, service, actor, invoke)
    integrator.register(server, service, actor, invoke)
    qa.register(server, service, actor, invoke)
    return server
