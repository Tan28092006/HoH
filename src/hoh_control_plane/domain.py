from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Role(StrEnum):
    PLANNER = "planner"
    DEVELOPER = "developer"
    QA = "qa"
    INTEGRATOR = "integrator"
    ADMIN = "admin"


class TicketStatus(StrEnum):
    BACKLOG = "BACKLOG"
    READY = "READY"
    DEV_IN_PROGRESS = "DEV_IN_PROGRESS"
    QA_READY = "QA_READY"
    QA_IN_PROGRESS = "QA_IN_PROGRESS"
    REWORK = "REWORK"
    VERIFIED = "VERIFIED"
    MERGE_READY = "MERGE_READY"
    DONE = "DONE"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"
    SUPERSEDED = "SUPERSEDED"


class WorkspaceModel(StrEnum):
    """Where the working tree a Developer edits actually lives.

    COLOCATED is the original assumption and stays the default: the agent runs
    beside the service, so a service-provisioned worktree path is one the agent
    can open. AGENT_WORKSPACE is for an agent on another machine, which cannot
    reach any path the service creates for itself.
    """

    COLOCATED = "colocated"
    AGENT_WORKSPACE = "agent_workspace"


class PlannerJobStatus(StrEnum):
    READY = "READY"
    IN_PROGRESS = "IN_PROGRESS"
    RESOLVED = "RESOLVED"


class PlannerJobType(StrEnum):
    QA_FAILURE = "qa_failure"
    DEVELOPER_BLOCKER = "developer_blocker"
    INTEGRATION_CONFLICT = "integration_conflict"


class InterventionStatus(StrEnum):
    """Delivery state of a Planner wakeup, never the state of the decision itself."""

    PENDING = "PENDING"
    DELIVERED = "DELIVERED"
    ACKNOWLEDGED = "ACKNOWLEDGED"


class InterventionSeverity(StrEnum):
    HIGH = "high"
    NORMAL = "normal"


# A retired ticket keeps its history but leaves dispatch for good. These groups are
# the shared lifecycle contract: the wider Planner lifecycle work extends them
# rather than restating which states may be retired or are already closed.
RETIRABLE_TICKET_STATUSES = frozenset(
    {TicketStatus.BACKLOG, TicketStatus.READY, TicketStatus.BLOCKED}
)
CLOSED_TICKET_STATUSES = frozenset({TicketStatus.CANCELLED, TicketStatus.SUPERSEDED})


@dataclass(frozen=True, slots=True)
class Actor:
    agent_id: str
    role: Role
    project_ids: frozenset[str]

    def can_access(self, project_id: str) -> bool:
        return "*" in self.project_ids or project_id in self.project_ids


class DomainError(Exception):
    code = "domain_error"
    status_code = 400

    def __init__(self, message: str, details: dict[str, object] | None = None) -> None:
        super().__init__(message)
        self.details = details or {}


class AuthorizationError(DomainError):
    code = "forbidden"
    status_code = 403


class AuthenticationError(DomainError):
    code = "unauthorized"
    status_code = 401


class ConflictError(DomainError):
    code = "state_conflict"
    status_code = 409


class ReleasePolicyError(ConflictError):
    """The current candidate does not satisfy the standing release policy.

    Distinct from a plain state conflict because the caller needs to know which
    gate refused; the machine-readable reason travels in details["reason"].
    """

    code = "release_policy_blocked"


class IdempotencyConflictError(ConflictError):
    """The caller reused an idempotency key that cannot replay this exact request.

    Distinct from a plain state conflict so a client can tell "your retry was
    refused" from "the domain state moved on"; the details carry which guard fired.
    """

    code = "idempotency_conflict"


class SkillCompatibilityError(ConflictError):
    """The registered agent's declared skill or protocol version fails project policy.

    Distinct from a plain state conflict so a client can tell "your identity is
    fine but your tooling is stale" from an ordinary version race; the details
    carry which skill or protocol failed and why.
    """

    code = "skill_incompatible"


class ContextStaleError(ConflictError):
    """The context this write was decided from no longer describes current state.

    Distinct from a plain version conflict because the caller did not
    necessarily send a wrong expected_version: it may have read a correct
    packet, then acted on it after the world moved. details["stale"] names which
    parts to refresh so the caller reloads those rather than everything.
    """

    code = "context_stale"


class NotFoundError(DomainError):
    code = "not_found"
    status_code = 404


class ValidationError(DomainError):
    code = "validation_error"
    status_code = 422


def require_role(actor: Actor, *roles: Role) -> None:
    if actor.role not in roles:
        allowed = ", ".join(role.value for role in roles)
        raise AuthorizationError(
            f"Role '{actor.role.value}' cannot perform this action; expected {allowed}"
        )


def require_project(actor: Actor, project_id: str) -> None:
    if not actor.can_access(project_id):
        raise AuthorizationError("Actor cannot access this project")
