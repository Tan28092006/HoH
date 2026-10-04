"""`hoh-mcp` entry point. The transport lives in the `hoh_control_plane.mcp` package."""
from __future__ import annotations

import os
import subprocess  # noqa: F401  (tests patch subprocess.Popen through this module)

from mcp.server.fastmcp.exceptions import ToolError  # noqa: F401

from hoh_control_plane.auth import (
    STDIO_TRANSPORT,
    TOKENS_VARIABLE,
    credentials_from_environment,
    requested_transport,
)
from hoh_control_plane.domain import Actor, Role
from hoh_control_plane.mcp.http_app import create_mcp_http_app
from hoh_control_plane.mcp.invoke import (  # noqa: F401
    _CompactFastMCP,
    _project_for_call,
    _run_with_contention_retry,
    _safe_input_summary,
    _tool_error_text,
)
from hoh_control_plane.mcp.server import create_mcp_server
from hoh_control_plane.mcp.workers import (  # noqa: F401
    _start_candidate_submission_worker,
    _start_merge_worker,
    _start_qa_test_worker,
    _start_worktree_provision_worker,
)
from hoh_control_plane.service import ControlPlaneService
from hoh_control_plane.store_factory import store_from_environment


def main() -> None:
    store = store_from_environment()
    service = ControlPlaneService(store)
    transport = requested_transport()

    if transport != STDIO_TRANSPORT:
        credentials = credentials_from_environment()
        if not credentials:
            raise SystemExit(
                f"{TOKENS_VARIABLE} is required to serve MCP on a network transport; "
                "the environment is not trusted to grant a role off stdio"
            )
        import uvicorn

        provider = None
        if os.getenv('HOH_LOCAL_CREDENTIALS_PATH'):
            from hoh_control_plane.local_credentials import credential_provider
            provider = credential_provider(credentials, os.environ['HOH_LOCAL_CREDENTIALS_PATH'])

        uvicorn.run(
            create_mcp_http_app(service, credentials, transport, provider),
            host=os.getenv("HOH_MCP_HOST", "127.0.0.1"),
            port=int(os.getenv("HOH_MCP_PORT", "8931")),
            log_level="warning",
        )
        return

    agent_id = os.getenv("HOH_AGENT_ID")
    role_name = os.getenv("HOH_AGENT_ROLE")
    project_ids = os.getenv("HOH_PROJECT_IDS", "*")
    if not agent_id or not role_name:
        raise SystemExit("HOH_AGENT_ID and HOH_AGENT_ROLE are required")
    try:
        role = Role(role_name)
    except ValueError as exc:
        raise SystemExit(f"Unknown HOH_AGENT_ROLE: {role_name}") from exc
    actor = Actor(agent_id, role, frozenset(item.strip() for item in project_ids.split(",")))
    create_mcp_server(service, actor).run(transport=STDIO_TRANSPORT)


if __name__ == "__main__":
    main()
