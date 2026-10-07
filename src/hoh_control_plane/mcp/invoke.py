"""Call wrapper shared by every MCP tool: audit, idempotency, contention retry, error text."""
from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, TypeVar
from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent
from hoh_control_plane.observability import correlation_scope
from hoh_control_plane.domain import Actor, DomainError
from hoh_control_plane.mcp.errors import (
    HohToolError,
    error_text,
    map_error,
    root_cause,
    tool_error,
)
from hoh_control_plane.service import ControlPlaneService
from hoh_control_plane.service._common import agent_facing, delivered_json, require_snapshot_id
from hoh_control_plane.store import ContentionError


Result = TypeVar("Result")

# A ContentionError means one transaction never began (its write lock was never
# acquired), so that transaction left nothing to duplicate. It says nothing about
# the call's earlier transactions: a command such as a claim commits its effect
# and then writes again, and contention on the later write must not re-run the
# effect. So the whole call is retried only while nothing it wrote has
# committed, which the audit trail answers (`request_has_committed`). In practice
# `run_idempotent` already turns contention after a committed effect into the
# committed outcome, so a ContentionError reaching this layer is normally one
# with nothing committed; the check here keeps the rule true on its own.
# store.py already retries lock acquisition internally before raising, so this is
# a second, coarser bounded layer.
_CONTENTION_RETRY_ATTEMPTS = 3
_CONTENTION_RETRY_BASE_SECONDS = 0.1


def _run_with_contention_retry(
    operation: Callable[[], Result], committed: Callable[[], bool] = lambda: False
) -> Result:
    delay = _CONTENTION_RETRY_BASE_SECONDS
    for attempt in range(_CONTENTION_RETRY_ATTEMPTS):
        try:
            return operation()
        except ContentionError:
            if attempt == _CONTENTION_RETRY_ATTEMPTS - 1 or committed():
                raise
            time.sleep(delay)
            delay *= 2
    raise AssertionError("unreachable")  # pragma: no cover


_TARGET_KEYS = (
    "project_id", "feature_id", "ticket_id", "feature_ref", "ticket_ref", "note_ref"
)


class _CompactFastMCP(FastMCP):
    """FastMCP whose results are compact JSON and whose failures have one shape.

    The structured content is unchanged. The text block is what most clients
    put in front of the model, so it is the form the packet budget measures
    (`delivered_json`); indentation alone cost about a third more tokens.

    Every failure leaves `call_tool` as a HohToolError (mcp.errors), audited
    exactly once; over a transport it becomes an `isError` result whose
    structured content is `{code, message, details, retry_hint}`.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        #: Records a failure that never reached `invoke` (an argument the schema
        #: refused, a check made before the call): (tool, arguments, error).
        self.audit_failure: Callable[[str, dict[str, Any], HohToolError], None] | None = None
        self._mcp_server.call_tool(validate_input=False)(self._call_tool_result)

    async def _call_tool_result(self, name: str, arguments: dict[str, Any]) -> Any:
        try:
            return await self.call_tool(name, arguments)
        except HohToolError as error:
            return CallToolResult(
                content=[TextContent(type="text", text=str(error))],
                structuredContent=error.payload,
                isError=True,
            )

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        try:
            result = await super().call_tool(name, arguments)
        except Exception as raised:
            cause = root_cause(raised)
            error = tool_error(cause, audited=False)
            if not error.audited and self.audit_failure is not None:
                self.audit_failure(name, arguments, error)
                error.audited = True
            raise error from cause
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
            with correlation_scope() as correlation_id:
                result = _run_with_contention_retry(
                    run, lambda: service.request_has_committed(actor, correlation_id)
                )
        except Exception as raised:
            # One mapping (mcp.errors) for every failure: the audit records the
            # same code the client reads, and no internal text leaves the call.
            error = tool_error(raised, audited=True)
            service.record_tool_call(
                actor,
                tool_name=tool_name,
                project_id=project_id,
                input_summary=summary,
                outcome=error.outcome,
                error_code=error.payload["code"],
            )
            raise error from raised
        service.record_tool_call(
            actor,
            tool_name=tool_name,
            project_id=project_id,
            input_summary=summary,
            outcome="succeeded",
        )
        return result

    return invoke


def make_failure_audit(
    service: ControlPlaneService, actor: Actor
) -> Callable[[str, dict[str, Any], HohToolError], None]:
    """Audit a failure that never reached `invoke`, under the code the client reads."""

    def audit(tool_name: str, arguments: dict[str, Any], error: HohToolError) -> None:
        try:
            service.record_tool_call(
                actor,
                tool_name=tool_name,
                project_id=_project_for_call(service, actor, arguments),
                input_summary=_safe_input_summary(arguments),
                outcome=error.outcome,
                error_code=error.payload["code"],
            )
        except Exception:  # pragma: no cover - the audit never masks the refusal
            pass

    return audit


def _tool_error_text(error: DomainError) -> str:
    """`[code] message {json}`: the REST error body's details, readable by MCP clients."""
    return error_text(map_error(error)[0])


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
