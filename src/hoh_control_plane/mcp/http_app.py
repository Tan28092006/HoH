"""HTTP transport: authenticate the bearer, then serve that actor's own MCP server."""
from __future__ import annotations

import anyio
import json
from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import Any
from hoh_control_plane.auth import AgentCredential, TOKENS_VARIABLE, resolve_actor
from hoh_control_plane.domain import AuthorizationError, ValidationError
from hoh_control_plane.service import ControlPlaneService
from hoh_control_plane.mcp.server import create_mcp_server


def create_mcp_http_app(
    service: ControlPlaneService,
    credentials: tuple[AgentCredential, ...],
    transport: str = "streamable-http",
    credentials_provider: Callable[[], tuple[AgentCredential, ...]] | None = None,
) -> Any:
    """Serve one authenticated MCP endpoint per configured credential.

    Each credential gets its own server instance bound to its own identity, so a
    role's tools are physically absent from the surface it reaches rather than
    merely refused when called. Authentication happens before dispatch, so an
    unauthenticated request never reaches a tool at all.
    """
    if not credentials:
        raise ValidationError(
            f"A network transport requires agent credentials in {TOKENS_VARIABLE}"
        )
    per_actor = {}
    group = None
    creation_lock = anyio.Lock()

    async def serve_child(child, *, task_status=anyio.TASK_STATUS_IGNORED):
        # Each lifespan is entered and exited by the same task (AnyIO scopes).
        async with child.router.lifespan_context(child):
            task_status.started()
            await anyio.sleep_forever()

    @asynccontextmanager
    async def lifespan(_app: Any):
        nonlocal group
        async with anyio.create_task_group() as running:
            group = running
            try:
                yield
            finally:
                running.cancel_scope.cancel()
                group = None
                per_actor.clear()

    from starlette.applications import Starlette

    lifecycle = Starlette(lifespan=lifespan)

    async def refuse(scope: Any, receive: Any, send: Any, detail: str) -> None:
        body = json.dumps({"error": {"code": "unauthorized", "message": detail}})
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"www-authenticate", b"Bearer"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body.encode("utf-8")})

    async def app(scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "lifespan":
            await lifecycle(scope, receive, send)
            return
        if scope["type"] != "http":
            raise ValueError("Only HTTP scopes are served by this endpoint")
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        authorization = headers.get(b"authorization", b"").decode("latin-1") or None
        try:
            current = credentials_provider() if credentials_provider else credentials
            actor = resolve_actor(current, authorization)
        except (AuthorizationError, ValueError, OSError, ValidationError):
            # The reason is deliberately coarse: which of missing or wrong is not
            # information an unauthenticated caller has earned.
            await refuse(scope, receive, send, 'A valid bearer credential is required')
            return
        async with creation_lock:
            if actor not in per_actor:
                if group is None:
                    raise RuntimeError('MCP HTTP lifespan has not started')
                server = create_mcp_server(service, actor, stateless_http=transport == 'streamable-http')
                child = server.sse_app() if transport == 'sse' else server.streamable_http_app()
                await group.start(serve_child, child)
                per_actor[actor] = child
        await per_actor[actor](scope, receive, send)

    return app
