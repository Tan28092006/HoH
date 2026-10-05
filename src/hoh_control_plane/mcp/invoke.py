"""Call wrapper shared by every MCP tool: audit, idempotency, contention retry, error text."""
from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable
from typing import Any, TypeVar
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import TextContent
from hoh_control_plane.observability import correlation_scope
from hoh_control_plane.domain import Actor, ConflictError, DomainError
from hoh_control_plane.service import ControlPlaneService
from hoh_control_plane.service._common import agent_facing, delivered_json, require_snapshot_id
from hoh_control_plane.store import ContentionError


Result = TypeVar("Result")

# ContentionError only ever comes from a transaction that never began (the write
# lock was never acquired), so no partial mutation exists to duplicate; retrying
# the whole call from scratch is safe. store.py already retries lock acquisition
# internally before raising it, so this is a second, coarser bounded layer.
_CONTENTION_RETRY_ATTEMPTS = 3
_CONTENTION_RETRY_BASE_SECONDS = 0.1


def _run_with_contention_retry(operation: Callable[[], Result]) -> Result:
    delay = _CONTENTION_RETRY_BASE_SECONDS
    for attempt in range(_CONTENTION_RETRY_ATTEMPTS):
        try:
            return operation()
        except ContentionError:
            if attempt == _CONTENTION_RETRY_ATTEMPTS - 1:
                raise
            time.sleep(delay)
            delay *= 2
    raise AssertionError("unreachable")  # pragma: no cover


_TARGET_KEYS = (
    "project_id", "feature_id", "ticket_id", "feature_ref", "ticket_ref", "note_ref"
)


class _CompactFastMCP(FastMCP):
    """FastMCP whose text content is compact JSON instead of `indent=2`.

    The structured content is unchanged. The text block is what most clients
    put in front of the model, so it is the form the packet budget measures
    (`delivered_json`); indentation alone cost about a third more tokens.
    """

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        result = await super().call_tool(name, arguments)
        if isinstance(result, tuple) and len(result) == 2 and isinstance(result[1], dict):
            _content, structured = result
            # Every result an agent reads names commits and digests in short form.
            structured = agent_facing(structured)
            # A non-dict return value is wrapped as {"result": value} by FastMCP.
            payload = structured["result"] if set(structured) == {"result"} else structured
            return [TextContent(type="text", text=delivered_json(payload))], structured
        return result


def make_invoke(service: ControlPlaneService, actor: Actor) -> Callable[..., Any]:
    """Bind the audited, idempotent, contention-retried call wrapper to one actor."""

    def invoke(
        tool_name: str,
        arguments: dict[str, Any],
        operation: Callable[[], Result],
        idempotency_key: str | None = None,
    ) -> Result:
        project_id = _project_for_call(service, actor, arguments)
        summary = _safe_input_summary(arguments)

        def run() -> Result:
            return service.run_idempotent(
                actor,
                operation=tool_name,
                idempotency_key=idempotency_key,
                payload=arguments,
                run=operation,
                project_id=project_id,
            )

        try:
            with correlation_scope():
                result = _run_with_contention_retry(run)
        except DomainError as error:
            service.record_tool_call(
                actor,
                tool_name=tool_name,
                project_id=project_id,
                input_summary=summary,
                outcome="rejected",
                error_code=error.code,
            )
            # Stable, machine-readable parity with the REST error body: a stdio
            # client branches on the code instead of parsing the prose.
            raise ToolError(_tool_error_text(error)) from error
        except (sqlite3.OperationalError, sqlite3.IntegrityError) as error:
            # Safety net: normalize any raw driver error that reaches this boundary
            # without an existing service-layer translation (e.g. a foreign-key
            # violation this call site has no bespoke message for), so a caller
            # never sees internal storage-engine text through MCP.
            service.record_tool_call(
                actor,
                tool_name=tool_name,
                project_id=project_id,
                input_summary=summary,
                outcome="rejected",
                error_code="database_error",
            )
            raise ToolError(
                f"[{ConflictError.code}] This change conflicts with the current database "
                "state; retry the request."
            ) from error
        except Exception:
            service.record_tool_call(
                actor,
                tool_name=tool_name,
                project_id=project_id,
                input_summary=summary,
                outcome="failed",
                error_code="internal_error",
            )
            raise
        service.record_tool_call(
            actor,
            tool_name=tool_name,
            project_id=project_id,
            input_summary=summary,
            outcome="succeeded",
        )
        return result

    return invoke


_RETRY_HINTS = {
    "context_stale": "Re-read ticket.get_context, then retry with its context_snapshot_id.",
    "repository_reconciliation_required": (
        "Planner must reconcile first; call work.wait_for_event, then retry."
    ),
    "lease_expired": "The lease is gone; call work.discover and claim again.",
}


def _retry_hint(error: DomainError) -> str | None:
    if error.code in _RETRY_HINTS:
        return _RETRY_HINTS[error.code]
    reason = error.details.get("reason") if isinstance(error.details, dict) else None
    return _RETRY_HINTS.get(str(reason)) if reason else None


def _tool_error_text(error: DomainError) -> str:
    """`[code] message {json}`: the REST error body's details, readable by MCP clients."""
    text = f"[{error.code}] {error}"
    extra: dict[str, Any] = {}
    if error.details:
        extra["details"] = error.details
    hint = _retry_hint(error)
    if hint:
        extra["retry_hint"] = hint
    if extra:
        text += " " + json.dumps(extra, separators=(",", ":"), sort_keys=True, default=str)
    return text


def _safe_input_summary(arguments: dict[str, Any]) -> dict[str, Any]:
    """Keep only operation shape and opaque resource references; never persist content."""
    target = {
        key: value
        for key, value in arguments.items()
        if key in _TARGET_KEYS and value not in (None, "")
    }
    summary: dict[str, Any] = {
        "argument_keys": sorted(key for key, value in arguments.items() if value is not None)
    }
    if target:
        summary["target"] = target
    return summary


def _project_for_call(
    service: ControlPlaneService, actor: Actor, arguments: dict[str, Any]
) -> str | None:
    """Resolve the log partition without changing tool success or failure behavior."""
    project_id = arguments.get("project_id")
    if isinstance(project_id, str) and project_id:
        return project_id
    try:
        feature_id = arguments.get("feature_id") or arguments.get("feature_ref")
        if isinstance(feature_id, str) and feature_id:
            return service.project_id_for_feature(actor, _strip_reference(feature_id, "feature"))
        ticket_id = arguments.get("ticket_id") or arguments.get("ticket_ref")
        if isinstance(ticket_id, str) and ticket_id:
            return service.project_id_for_ticket(actor, _strip_reference(ticket_id, "ticket"))
    except DomainError:
        return None
    project_ids = sorted(project_id for project_id in actor.project_ids if project_id != "*")
    return project_ids[0] if len(project_ids) == 1 else None


def _require_snapshot(context_snapshot_id: Any, tool_name: str) -> str:
    """Fail fast at the transport; the service enforces the same rule."""
    return require_snapshot_id(context_snapshot_id, tool_name)


def _strip_reference(reference: str, kind: str) -> str:
    context_prefix = f"ctx:{kind}:"
    if reference.startswith(context_prefix):
        return reference[len(context_prefix) :].rsplit(":v", 1)[0]
    prefix = f"{kind}:"
    return reference[len(prefix) :] if reference.startswith(prefix) else reference
