"""The one place an MCP tool failure becomes what a client reads.

Every failure, whether a domain refusal, a storage error, an argument the schema
refused, a lost connection or a bug, leaves a tool as an `isError` result whose
structured content is `{code, message, details, retry_hint}` and whose text is
`[code] message {details, retry_hint}`. A client branches on `code`; it never
needs to parse prose, and it never sees internal exception text.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any

import pydantic
from mcp.server.fastmcp.exceptions import ToolError

from hoh_control_plane.domain import ConflictError, DomainError, ValidationError

logger = logging.getLogger(__name__)

INTERNAL_ERROR = "internal_error"
CONNECTION_LOST = "connection_lost"

_RETRY_HINTS = {
    "context_stale": "Re-read ticket.get_context, then retry with its context_snapshot_id.",
    "repository_reconciliation_required": (
        "Planner must reconcile first; call work.wait_for_event, then retry."
    ),
    "lease_expired": "The lease is gone; call work.discover and claim again.",
    "database_conflict": "Re-read the state you acted on, then retry the request once.",
    "arguments_invalid": (
        "Fix the named arguments; the tool's input schema lists the accepted values."
    ),
    CONNECTION_LOST: "Reconnect MCP and resume the same identity via session.bootstrap.",
}


class HohToolError(ToolError):
    """A mapped tool failure: carries the payload and whether it was audited."""

    def __init__(self, payload: dict[str, Any], *, outcome: str, audited: bool) -> None:
        super().__init__(error_text(payload))
        self.payload = payload
        self.outcome = outcome
        self.audited = audited


def _payload(
    code: str, message: str, details: dict[str, Any] | None = None, hint_key: str | None = None
) -> dict[str, Any]:
    details = details or {}
    hint = _RETRY_HINTS.get(hint_key or code)
    if hint is None and isinstance(details.get("reason"), str):
        hint = _RETRY_HINTS.get(details["reason"])
    return {"code": code, "message": message, "details": details, "retry_hint": hint}


def _argument_errors(error: pydantic.ValidationError) -> list[dict[str, str]]:
    """Which argument failed and why, without echoing the value that was sent."""
    issues = []
    for issue in error.errors(include_url=False, include_input=False, include_context=False):
        location = [str(part) for part in issue["loc"]]
        # FastMCP validates one generated model whose fields are the arguments.
        if location and location[0].endswith("Arguments"):
            location = location[1:]
        issues.append({
            "field": ".".join(location),
            "message": issue["msg"],
            "code": issue["type"],
        })
    return issues[:20]


def map_error(error: BaseException) -> tuple[dict[str, Any], str]:
    """The client payload and the audit outcome for one failure."""
    if isinstance(error, HohToolError):
        return error.payload, error.outcome
    if isinstance(error, DomainError):
        return _payload(error.code, str(error), dict(error.details or {})), "rejected"
    if isinstance(error, sqlite3.Error):
        # Never the storage engine's text: it names tables and constraints.
        return _payload(
            ConflictError.code,
            "This change conflicts with the current database state; retry the request.",
            {"reason": "database_conflict"},
        ), "rejected"
    if isinstance(error, pydantic.ValidationError):
        issues = _argument_errors(error)
        fields = ", ".join(issue["field"] or "arguments" for issue in issues)
        return _payload(
            ValidationError.code,
            f"Invalid arguments: {fields}",
            {"reason": "arguments_invalid", "errors": issues},
        ), "rejected"
    if isinstance(error, ConnectionError):
        return _payload(
            CONNECTION_LOST, "The MCP connection was lost while the call was running."
        ), "failed"
    if isinstance(error, ToolError) and error.__cause__ is None:
        # FastMCP's own refusal (an unknown tool name): framework text, no internals.
        return _payload(ValidationError.code, str(error)), "rejected"
    logger.error("Unexpected MCP tool failure", exc_info=error)
    return _payload(
        INTERNAL_ERROR,
        "The control plane could not complete this request; the failure was logged.",
    ), "failed"


def error_text(payload: dict[str, Any]) -> str:
    """`[code] message {json}`: the readable form of the payload for text-only clients."""
    text = f"[{payload['code']}] {payload['message']}"
    extra = {
        key: payload[key] for key in ("details", "retry_hint") if payload.get(key)
    }
    if extra:
        text += " " + json.dumps(extra, separators=(",", ":"), sort_keys=True, default=str)
    return text


def tool_error(error: BaseException, *, audited: bool) -> HohToolError:
    """Map any failure to the one error a tool raises."""
    if isinstance(error, HohToolError):
        return error
    payload, outcome = map_error(error)
    return HohToolError(payload, outcome=outcome, audited=audited)


def root_cause(error: BaseException) -> BaseException:
    """Unwrap FastMCP's "Error executing tool" ToolError to what the tool raised."""
    if (
        isinstance(error, ToolError)
        and not isinstance(error, HohToolError)
        and error.__cause__ is not None
    ):
        return error.__cause__
    return error
