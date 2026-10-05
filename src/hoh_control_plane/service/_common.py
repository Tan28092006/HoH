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

from hoh_control_plane import contracts
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
from hoh_control_plane.observability import current_correlation_id
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
# The values live in hoh_control_plane.contracts, which the MCP and REST
# schemas are built from, so the three cannot drift.
_CONTRACT_TEXT_CHARS = contracts.CONTRACT_TEXT_CHARS
_CONTRACT_TOKEN_BUDGET = contracts.CONTRACT_TOKEN_BUDGET

# A maker and a checker can disagree forever. The third QA rejection of one
# ticket stops the loop: the ticket is BLOCKED and Planner decides whether to
# split it, revise its contract or cancel it. Only a recorded Planner decision
# starts a new count (event ticket.rework_budget_reset).
MAX_QA_REJECTIONS = 3
REWORK_BUDGET_EXHAUSTED = "rework_budget_exhausted"

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


# What an agent reads names a commit by 12 hex characters, as `git log` would.
# A full SHA-1 costs about ten tokens and a SHA-256 digest about sixteen, every
# time a packet or result repeats one, and no agent compares them by eye. The
# store, audit events and evidence reports keep the full value; tools that take
# a commit accept either form and resolve a prefix before any comparison.
AGENT_SHA_CHARS = 12
_FULL_HEX = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_EMBEDDED_HEX = re.compile(r"(?<![0-9A-Za-z])[0-9a-f]{40}(?:[0-9a-f]{24})?(?![0-9A-Za-z])")


def agent_facing(payload: Any) -> Any:
    """Abbreviate every full commit SHA and SHA-256 digest in what an agent reads.

    A whole value that is a 40 or 64 hex string is shortened. Inside prose (a
    value with whitespace) embedded ones are shortened too; a value without
    whitespace is a reference an agent may pass back, so only an exact match is
    touched. Idempotent: an abbreviated value no longer matches.
    """
    if isinstance(payload, str):
        if _FULL_HEX.fullmatch(payload):
            return payload[:AGENT_SHA_CHARS]
        if any(character.isspace() for character in payload):
            return _EMBEDDED_HEX.sub(lambda match: match.group(0)[:AGENT_SHA_CHARS], payload)
        return payload
    if isinstance(payload, dict):
        return {key: agent_facing(value) for key, value in payload.items()}
    if isinstance(payload, (list, tuple)):
        return [agent_facing(value) for value in payload]
    return payload


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


def _write_audit_event(
    connection: Connection,
    actor: Actor,
    project_id: str,
    event_type: str,
    aggregate_type: str,
    aggregate_id: str,
    aggregate_version: int,
    payload: dict[str, Any],
) -> None:
    """Append one audit event in the caller's transaction (``_event`` delegates here)."""
    correlation_id = current_correlation_id()
    # The request that last moved this aggregate is what caused this one to
    # act, which is what links a chain across worker processes: each step
    # read the state the previous step left.
    previous = connection.execute(
        """
        SELECT correlation_id FROM audit_events
        WHERE aggregate_type = ? AND aggregate_id = ? AND correlation_id IS NOT NULL
        ORDER BY sequence DESC LIMIT 1
        """,
        (aggregate_type, aggregate_id),
    ).fetchone()
    causation_id = previous["correlation_id"] if previous else None
    if causation_id == correlation_id:
        # Several events from one request are siblings, not cause and effect.
        causation_id = None
    connection.execute(
        """
        INSERT INTO audit_events
            (event_id, event_type, aggregate_type, aggregate_id, aggregate_version,
             project_id, actor_id, actor_role, payload, correlation_id, causation_id,
             occurred_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            _id(connection, "event"),
            event_type,
            aggregate_type,
            aggregate_id,
            aggregate_version,
            project_id,
            actor.agent_id,
            actor.role.value,
            json.dumps(payload),
            correlation_id,
            causation_id,
            _iso(),
        ),
    )


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


class ExecutionGate:
    """The repository reconciliation barrier, read in one place.

    While any reconciliation is PENDING for a project, no new offer, claim,
    submission or merge starts. Every call site asks this class.
    """

    @staticmethod
    def is_closed(connection: Connection, project_id: str) -> bool:
        return connection.execute(
            """
            SELECT 1 FROM repository_reconciliations
            WHERE project_id = ? AND status = 'PENDING' LIMIT 1
            """,
            (project_id,),
        ).fetchone() is not None

    @staticmethod
    def state(
        connection: Connection, project_id: str
    ) -> dict[str, Any]:
        rows = connection.execute(
            """
            SELECT reconciliation_id, observation_ref, observed_commits, created_at
            FROM repository_reconciliations
            WHERE project_id = ? AND status = 'PENDING'
            ORDER BY created_at ASC
            """,
            (project_id,),
        ).fetchall()
        pending = []
        for row in rows:
            value = _row(row) or {}
            commits = value.pop("observed_commits")
            value["observed_commit_count"] = len(commits)
            value["commit_refs"] = [item.get("commit") for item in commits[:20]]
            value["opened_at"] = value.pop("created_at", None)
            pending.append(value)
        return {"repository_reconciliation_required": bool(pending), "pending": pending}

    @classmethod
    def require_open(
        cls, connection: Connection, project_id: str
    ) -> None:
        state = cls.state(connection, project_id)
        if state["repository_reconciliation_required"]:
            raise ConflictError(
                "Repository changed after the current plan was formed; Planner must reconcile every open ticket before execution continues",
                {"reason": "repository_reconciliation_required", **state},
            )


#: Ticket columns a lifecycle write may set. ``version`` is not here: the
#: transition sets it, always to ``expected_version + 1``.
_TRANSITION_COLUMNS = frozenset({
    "status", "blocked_reason", "base_commit", "candidate_commit", "updated_at",
    "title", "objective", "priority", "acceptance_criteria", "validation_contract",
    "preservation_constraints", "file_scope",
})

#: The one event type that records a ticket's state. A ticket's creation and
#: every ``_transition_ticket`` write it in the same transaction, so folding
#: these events in sequence order reproduces every ticket's status and version
#: (``replay_ticket_states``). Domain events (``ticket.claimed``,
#: ``candidate.frozen`` ...) still say what happened; this one says what the row
#: became, and is the only event a replay reads.
TICKET_TRANSITION_EVENT = "ticket.transitioned"
#: Changed values a transition event carries besides status. Commits identify
#: the artifact; the reason is truncated because it can hold a whole error.
_TRANSITION_EVENT_VALUES = ("base_commit", "candidate_commit", "blocked_reason")
_TRANSITION_REASON_CHARS = 300


def _ticket_project_and_status(connection: Connection, ticket_id: str) -> tuple[str, str] | None:
    row = connection.execute(
        """
        SELECT f.project_id, t.status FROM tickets t
        JOIN features f ON f.feature_id = t.feature_id
        WHERE t.ticket_id = ?
        """,
        (ticket_id,),
    ).fetchone()
    return None if row is None else (str(row["project_id"]), str(row["status"]))


def _record_ticket_created(connection: Connection, actor: Actor, ticket_id: str) -> None:
    """The initial transition, from nothing to version 1, so a fold can start empty."""
    located = _ticket_project_and_status(connection, ticket_id)
    if located is None:
        raise NotFoundError("Ticket not found")
    project_id, status = located
    _write_audit_event(
        connection, actor, project_id, TICKET_TRANSITION_EVENT, "ticket", ticket_id, 1,
        {
            "from_status": None,
            "to_status": status,
            "from_version": 0,
            "to_version": 1,
            "changed_fields": ["created"],
        },
    )


def _transition_ticket(
    connection: Connection,
    ticket_id: str,
    *,
    actor: Actor,
    expected_version: int,
    from_statuses: Any,
    changes: dict[str, Any],
) -> int:
    """Write one ticket change guarded at the SQL level, and return the new version.

    The UPDATE matches only while the row still has ``expected_version`` and one
    of ``from_statuses``. Every caller has already checked both in Python, so
    under the global write lock this never refuses anything; it is what keeps a
    writer outside that lock (a non-``immediate`` connection, or PostgreSQL row
    locks) from silently overwriting a newer ticket state.

    The same transaction appends a ``ticket.transitioned`` event (from and to
    status and version, the changed fields), written by ``actor``. That event
    is the canonical record of the change; whatever domain event the caller
    adds says why.
    """
    if not changes:
        raise ValueError("A ticket transition must change at least one column")
    unknown = set(changes) - _TRANSITION_COLUMNS
    if unknown:
        raise ValueError(f"Not a ticket transition column: {', '.join(sorted(unknown))}")
    statuses = tuple(
        status.value if isinstance(status, TicketStatus) else TicketStatus(status).value
        for status in from_statuses
    )
    if not statuses:
        raise ValueError("A ticket transition needs at least one source status")
    values = {
        column: (value.value if isinstance(value, TicketStatus) else value)
        for column, value in changes.items()
    }
    if "status" in values:
        values["status"] = TicketStatus(values["status"]).value
    next_version = int(expected_version) + 1
    assignments = ", ".join(f"{column} = ?" for column in values)
    placeholders = ", ".join("?" for _ in statuses)
    located = _ticket_project_and_status(connection, ticket_id)
    cursor = connection.execute(
        f"UPDATE tickets SET {assignments}, version = ? "
        f"WHERE ticket_id = ? AND version = ? AND status IN ({placeholders})",
        (*values.values(), next_version, ticket_id, int(expected_version), *statuses),
    )
    if cursor.rowcount != 1:
        current = connection.execute(
            "SELECT status, version FROM tickets WHERE ticket_id = ?", (ticket_id,)
        ).fetchone()
        raise ConflictError(
            "Ticket changed before this write could apply; read it again",
            {
                "reason": "ticket_transition_conflict",
                "ticket_id": ticket_id,
                "expected_version": int(expected_version),
                "expected_statuses": list(statuses),
                "current_version": None if current is None else current["version"],
                "current_status": None if current is None else current["status"],
            },
        )
    assert located is not None  # the UPDATE matched the row
    project_id, from_status = located
    payload: dict[str, Any] = {
        "from_status": from_status,
        "to_status": values.get("status", from_status),
        "from_version": int(expected_version),
        "to_version": next_version,
        "changed_fields": sorted(column for column in values if column != "updated_at"),
    }
    for column in _TRANSITION_EVENT_VALUES:
        if column in values:
            value = values[column]
            if column == "blocked_reason" and isinstance(value, str):
                value = value[:_TRANSITION_REASON_CHARS]
            payload[column] = value
    _write_audit_event(
        connection, actor, project_id, TICKET_TRANSITION_EVENT, "ticket", ticket_id,
        next_version, payload,
    )
    return next_version


def replay_ticket_states(connection: Connection, project_id: str) -> dict[str, dict[str, Any]]:
    """Fold a project's ``ticket.transitioned`` events into each ticket's status and version.

    Events are applied in sequence order. Each one must continue the version
    its ticket reached (``from_version``) and start from the status it left
    (``from_status``); a gap or mismatch is reported on that ticket as
    ``replay_errors`` rather than raised, so an audit tool can list every
    divergence at once. Compare the result with the ``tickets`` rows to see
    whether the log explains the current state.
    """
    states: dict[str, dict[str, Any]] = {}
    rows = connection.execute(
        """
        SELECT sequence, aggregate_id, aggregate_version, payload FROM audit_events
        WHERE project_id = ? AND aggregate_type = 'ticket' AND event_type = ?
        ORDER BY sequence ASC
        """,
        (project_id, TICKET_TRANSITION_EVENT),
    ).fetchall()
    for row in rows:
        event = _row(row) or {}
        payload = event["payload"] if isinstance(event.get("payload"), dict) else {}
        ticket_id = str(event["aggregate_id"])
        state = states.setdefault(
            ticket_id, {"status": None, "version": 0, "transitions": 0, "replay_errors": []}
        )
        if payload.get("from_version") != state["version"]:
            state["replay_errors"].append(
                f"sequence {event['sequence']}: from_version {payload.get('from_version')} "
                f"after version {state['version']}"
            )
        elif payload.get("from_status") != state["status"]:
            state["replay_errors"].append(
                f"sequence {event['sequence']}: from_status {payload.get('from_status')} "
                f"after status {state['status']}"
            )
        state["status"] = payload.get("to_status")
        state["version"] = payload.get("to_version", event["aggregate_version"])
        state["transitions"] += 1
    return states
