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
    "skill_manifest",
    "skill_compatibility",
    "accepted_protocol_versions",
    "accepted_skill_versions",
}
COMMIT_RE = re.compile(r"^[0-9a-fA-F]{40,64}$")

MIN_TOKEN_BUDGET = 200
MAX_TOKEN_BUDGET = 200_000
DEFAULT_TOKEN_BUDGET = 8_000
_CHECKPOINT_CHARS = 200

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
        "integration_base_drift",
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
_PROTECTED_SECTIONS = frozenset({"ticket", "candidate", "known_failures", "verification_profile"})

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


# 64 bits of the UUID rather than all 128. These identifiers are quoted in
# decisions, evidence references and handoff prose far more often than they are
# generated, so half the length is a real readability gain, while 2^64 keeps a
# collision far below the point where the primary key would ever notice. Existing
# longer identifiers stay valid: an id is opaque and is never parsed for length.
_ID_HEX_CHARS = 16


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:_ID_HEX_CHARS]}"


def _serialize(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, default=str)


def _estimate_tokens(payload: Any) -> int:
    """Deterministic size proxy; exact tokenizer parity is not what the budget guards."""
    return len(_serialize(payload)) // 4


def _snapshot_id(payload: dict[str, Any]) -> str:
    """Identify a compiled packet by its content so equal state compiles equal."""
    material = {
        key: value
        for key, value in payload.items()
        if key not in {"context_snapshot_id", "generated_at"}
    }
    digest = hashlib.sha256(_serialize(material).encode("utf-8")).hexdigest()
    return f"ctxsnap_{digest[:32]}"


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
        result[field] = json.loads(result[field])
    return result
