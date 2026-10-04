from __future__ import annotations

import base64
import hashlib
import json
import re
import sqlite3
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
import secrets
from uuid import uuid4

from hoh_control_plane.domain import (
    CLOSED_TICKET_STATUSES,
    RETIRABLE_TICKET_STATUSES,
    Actor,
    AuthorizationError,
    ConflictError,
    ContextStaleError,
    DomainError,
    IdempotencyConflictError,
    InterventionSeverity,
    InterventionStatus,
    NotFoundError,
    PlannerJobStatus,
    PlannerJobType,
    ReleasePolicyError,
    Role,
    SkillCompatibilityError,
    TicketStatus,
    ValidationError,
    WorkspaceModel,
    require_project,
    require_role,
)
from hoh_control_plane.git_worktrees import CLEAN_CHECKPOINT, DIRTY, GitWorktreeManager
from hoh_control_plane.qa_runner import ProjectTestRunner, TestExecution
from hoh_control_plane.store import (
    CHECKSUM_ARTIFACT,
    CHECKSUM_CLAIMED,
    SQLiteStore,
    StoreConnection as Connection,
    StoreIntegrityError,
)


_SAFE_PATH_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")

JSON_FIELDS = {
    "acceptance_criteria",
    "validation_contract",
    "verification_profile",
    "preservation_constraints",
    "file_scope",
    "developer_claims",
    "evidence_refs",
    "test_command",
    "payload",
    "input_summary",
    "project_ids",
    "skills",
    "execution_capabilities",
    "approved_execution_capabilities",
    "capability_approval_refs",
    "skill_manifest",
    "skill_compatibility",
    "observed_commits",
    "ticket_versions",
    "decision_manifest",
    "accepted_protocol_versions",
    "accepted_skill_versions",
}
COMMIT_RE = re.compile(r"^[0-9a-fA-F]{40,64}$")

MIN_TOKEN_BUDGET = 200
MAX_TOKEN_BUDGET = 200_000
DEFAULT_TOKEN_BUDGET = 4_000
_CHECKPOINT_CHARS = 200

# A validation contract lives in the protected ticket section of every packet
# for the ticket's whole life, so it is bounded at write time rather than
# compacted at read time. Six scenarios of typical length fit; a contract that
# does not fit is a ticket that should be split.
_CONTRACT_TEXT_CHARS = 600
_CONTRACT_TOKEN_BUDGET = 1_200

# Retention policy for idempotency records: a replay is honored for this long,
# after which purge_expired_idempotency_records may delete the row and the same
# key behaves like a fresh request. Long enough to outlive any worker restart
# or operator retry, short enough that keys are not kept indefinitely.
IDEMPOTENCY_RETENTION_SECONDS = 86_400
_MAX_IDEMPOTENCY_KEY_CHARS = 200

# Narration accumulates without bound; every other entry type is a durable
# decision that compaction may shorten but must never drop. A machine-written
# command status belongs here too: it restates what qa_test_runs already holds,
# so carrying it at full length in place of a decision is pure loss.
_VERBOSE_ENTRY_TYPES = frozenset(
    {"progress_update", "qa_test_run", "qa_automated_test_run"}
)

# A long-lived feature accumulates verified behaviour without bound. A new ticket
# needs to know what it must not break, not the feature's entire proven history,
# so the inherited set is capped and the most recently proven wins the room.
_MAX_INHERITED_CONSTRAINTS = 10

# The vocabulary a role can actually reason from. Without one, every situation
# grows its own label — the live control plane reached 41 types for 206 entries,
# most used exactly once — and a type that appears once can be neither filtered
# nor compacted, which is the whole point of typing an entry.
CONTEXT_ENTRY_TYPES = frozenset(
    {
        # Written by the control plane itself.
        "progress_update",
        "developer_blocker",
        "known_failure",
        "qa_test_run",
        "qa_automated_test_run",
        "write_scope_updated",
        "ticket_fields_updated",
        "worktree_handoff",
        "worktree_recovery",
        "integration_base_drift",
        "integration_regression_failed",
        "integration_authorization",
        "bootstrap_integration",
        # Written by a Planner.
        "planner_decision",
        "design_decision",
        "risk",
        "note",
    }
)

# Labels that mean exactly what a vocabulary type means. Mapping these is a
# rename and loses nothing, which is why they are not qualified onto the content
# the way a genuinely unknown label is.
CONTEXT_ENTRY_ALIASES = {
    "planning_decision": "planner_decision",
    "planner_decisions": "planner_decision",
    "decision": "planner_decision",
    "progress": "progress_update",
    "blocker": "developer_blocker",
}

# Where a label outside the vocabulary lands. Deliberately not a narration type,
# so an unrecognised entry is protected from compaction rather than discarded:
# an unknown label is a reason to keep something, not to throw it away.
GENERAL_CONTEXT_ENTRY_TYPE = "note"

# Artifact identity and open findings stay in every packet, so these sections
# are compacted to essentials rather than omitted.
_PROTECTED_SECTIONS = frozenset(
    {
        "ticket",
        "candidate",
        "known_failures",
        "verification_profile",
        "planning_barrier",
        "retirement",
    }
)

# What each role cannot act safely without: a Developer that lost its branch and
# base commit, or QA that lost the worktree it must execute in, would do damage
# a smaller packet never justifies.
_ROLE_REQUIRED_SECTIONS: dict[Role, frozenset[str]] = {
    Role.DEVELOPER: frozenset({"worktree"}),
    Role.QA: frozenset({"worktree", "test_runs"}),
    Role.PLANNER: frozenset({"feature", "dependencies"}),
    Role.INTEGRATOR: frozenset({"test_runs", "evidence_bundle"}),
}

# Least important last: compaction walks each list in reverse.
_TICKET_SECTION_PRIORITY: dict[Role, tuple[str, ...]] = {
    Role.DEVELOPER: (
        "worktree",
        "known_failures",
        "experience",
        "context_entries",
        "candidate",
        "dependencies",
        "feature",
        "test_runs",
        "inherited_constraints",
    ),
    Role.QA: (
        "candidate",
        "test_runs",
        "known_failures",
        "worktree",
        "context_entries",
        "dependencies",
        "feature",
        "experience",
        "inherited_constraints",
    ),
    Role.PLANNER: (
        "feature",
        "dependencies",
        "known_failures",
        "context_entries",
        "candidate",
        "test_runs",
        "worktree",
        "experience",
        "inherited_constraints",
    ),
    Role.INTEGRATOR: (
        "candidate",
        "known_failures",
        "test_runs",
        "feature",
        "dependencies",
        "worktree",
        "context_entries",
        "experience",
        "inherited_constraints",
    ),
}
_FALLBACK_SECTION_PRIORITY = (
    "candidate",
    "known_failures",
    "context_entries",
    "worktree",
    "dependencies",
    "feature",
    "test_runs",
    "experience",
    "inherited_constraints",
)


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None = None) -> str:
    return (value or _now()).isoformat().replace("+00:00", "Z")


# Identifiers are a prefix plus a per-prefix counter: cand_0007, lease_0042.
# They are quoted in packets, decisions, evidence references and tool calls far
# more often than they are generated, and every quotation is paid for in agent
# context. A 16-hex tail cost roughly ten tokens and was routinely mistyped by a
# model copying it back; four digits cost one or two tokens and read as an
# ordinal, so "the newer candidate" is visible from the id itself. The counter
# lives in the database under the same write lock as the row it names, which is
# why the caller passes its connection: uniqueness is a property of this store,
# not of randomness. Older random identifiers stay valid; an id is opaque and is
# never parsed for length. The counter widens past 9999 rather than wrapping.
_ID_DIGITS = 4


def _id(connection: Any, prefix: str) -> str:
    row = connection.execute(
        """
        INSERT INTO id_sequences (prefix, next_value) VALUES (?, 1)
        ON CONFLICT(prefix) DO UPDATE SET next_value = id_sequences.next_value + 1
        RETURNING next_value
        """,
        (prefix,),
    ).fetchone()
    return f"{prefix}_{int(row[0]):0{_ID_DIGITS}d}"


def _file_token() -> str:
    """A short random name for a file the store never indexes by identifier."""
    return secrets.token_hex(4)


def _serialize(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, default=str)


def _required_execution_capabilities(ticket: dict[str, Any]) -> set[str]:
    """Return the portable equipment needed to assess this ticket."""
    return {
        str(capability)
        for scenario in ticket.get("validation_contract") or []
        for capability in scenario.get("required_capabilities") or []
    }


def delivered_json(payload: Any) -> str:
    """The exact text an MCP tool result carries: compact, unescaped JSON."""
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False, default=str)


def _estimate_tokens(payload: Any) -> int:
    """Size of what the agent receives (`delivered_json`), at about four characters a token.

    Exact tokenizer parity is not what the budget guards; measuring the form
    actually delivered is.
    """
    return len(delivered_json(payload)) // 4


def _snapshot_digest(payload: dict[str, Any]) -> str:
    """Digest a compiled packet by its content so equal state compiles equal.

    The digest is how a re-read of unchanged context finds the snapshot row it
    already has; the identifier an agent quotes back is the row's short
    counter name, allocated when the digest is first seen.
    """
    material = {
        key: value
        for key, value in payload.items()
        if key not in {"context_snapshot_id", "generated_at"}
    }
    return hashlib.sha256(_serialize(material).encode("utf-8")).hexdigest()[:32]


# Same width as a real identifier, so the envelope is measured before the
# packet is fitted to its budget rather than appended afterwards.
_SNAPSHOT_PLACEHOLDER = "ctxsnap_0000"


def _checkpoint(text: str) -> str:
    if len(text) <= _CHECKPOINT_CHARS:
        return text
    return text[:_CHECKPOINT_CHARS] + "..."


def _validated_budget(token_budget: int | None) -> int:
    if token_budget is None:
        return DEFAULT_TOKEN_BUDGET
    if not MIN_TOKEN_BUDGET <= token_budget <= MAX_TOKEN_BUDGET:
        raise ValidationError(
            f"Context token budget must be between {MIN_TOKEN_BUDGET} and {MAX_TOKEN_BUDGET}"
        )
    return token_budget


def _safe_path_segment(value: str, field: str) -> str:
    if not _SAFE_PATH_SEGMENT_RE.fullmatch(value):
        raise ValidationError(f"{field} cannot be used in a recovery artifact path")
    return value


def _bounded_join(values: tuple[str, ...] | list[str], limit: int = 20) -> str:
    joined = ", ".join(values[:limit])
    if len(values) > limit:
        joined += f", and {len(values) - limit} more"
    return joined


def _row(row: Any | None) -> dict[str, Any] | None:
    if row is None:
        return None
    result = dict(row)
    for field in JSON_FIELDS & result.keys():
        if result[field] is not None and isinstance(result[field], str):
            result[field] = json.loads(result[field])
    return result


def _audit_event_has_field(
    connection: Any, aggregate_id: str, event_type: str, field: str, value: str
) -> bool:
    """True when an audit event's JSON payload has ``field`` exactly equal to ``value``.

    Audit payloads are evidence for authorization decisions, so they are parsed
    and compared field by field; a substring match would let ``x_1000`` match
    ``x_10000`` or any unrelated text that happens to contain the reference.
    """
    rows = connection.execute(
        "SELECT payload FROM audit_events WHERE aggregate_id = ? AND event_type = ?",
        (aggregate_id, event_type),
    ).fetchall()
    for row in rows:
        payload = row["payload"] if not isinstance(row, tuple) else row[0]
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except ValueError:
                continue
        if isinstance(payload, dict) and payload.get(field) == value:
            return True
    return False
def require_snapshot_id(context_snapshot_id: Any, tool_name: str) -> str:
    """A consequential write must name the packet it was decided from.

    The freshness check only runs against a snapshot the caller supplies, so an
    omitted snapshot silently skipped it. Submission, verdict and merge are the
    three writes whose staleness costs a whole iteration, so the service refuses
    them without one, whichever transport (MCP or REST) carried the request.
    """
    if not isinstance(context_snapshot_id, str) or not context_snapshot_id.strip():
        raise ValidationError(
            f"{tool_name} requires context_snapshot_id: call ticket.get_context "
            "immediately before this write and pass the context_snapshot_id it returns"
        )
    return context_snapshot_id.strip()
