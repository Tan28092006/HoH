from __future__ import annotations

from hoh_control_plane.service._common import (
    Actor,
    Any,
    AuthorizationError,
    CLEAN_CHECKPOINT,
    CLOSED_TICKET_STATUSES,
    COMMIT_RE,
    Callable,
    ConflictError,
    DEFAULT_TOKEN_BUDGET,
    DIRTY,
    DomainError,
    GitWorktreeManager,
    IDEMPOTENCY_RETENTION_SECONDS,
    IdempotencyConflictError,
    InterventionSeverity,
    InterventionStatus,
    JSON_FIELDS,
    MAX_TOKEN_BUDGET,
    MIN_TOKEN_BUDGET,
    NotFoundError,
    Path,
    PlannerJobStatus,
    PlannerJobType,
    ProjectTestRunner,
    RETIRABLE_TICKET_STATUSES,
    ReleasePolicyError,
    Role,
    SQLiteStore,
    SkillCompatibilityError,
    TestExecution,
    TicketStatus,
    UTC,
    ValidationError,
    _CHECKPOINT_CHARS,
    _FALLBACK_SECTION_PRIORITY,
    _MAX_IDEMPOTENCY_KEY_CHARS,
    _PROTECTED_SECTIONS,
    _ROLE_REQUIRED_SECTIONS,
    _SAFE_PATH_SEGMENT_RE,
    _TICKET_SECTION_PRIORITY,
    _VERBOSE_ENTRY_TYPES,
    _bounded_join,
    _checkpoint,
    _estimate_tokens,
    _id,
    _iso,
    _now,
    _row,
    _safe_path_segment,
    _serialize,
    _snapshot_id,
    _validated_budget,
    datetime,
    defaultdict,
    hashlib,
    json,
    re,
    require_project,
    require_role,
    timedelta,
    uuid4,
)
from hoh_control_plane.service.planning import PlanningMixin
from hoh_control_plane.service.discovery import DiscoveryMixin
from hoh_control_plane.service.context import ContextMixin
from hoh_control_plane.service.worktrees import WorktreeMixin
from hoh_control_plane.service.integration import IntegrationMixin
from hoh_control_plane.service.qa import QaMixin
from hoh_control_plane.service.candidates import CandidateMixin
from hoh_control_plane.service.developer import DeveloperMixin
from hoh_control_plane.service.board import BoardMixin
from hoh_control_plane.service.idempotency import IdempotencyMixin
from hoh_control_plane.service.planner_jobs import PlannerJobMixin
from hoh_control_plane.service.leases import LeaseMixin
from hoh_control_plane.service.validation import ValidationMixin


class ControlPlaneService(
    PlanningMixin,
    DiscoveryMixin,
    ContextMixin,
    WorktreeMixin,
    IntegrationMixin,
    QaMixin,
    CandidateMixin,
    DeveloperMixin,
    BoardMixin,
    IdempotencyMixin,
    PlannerJobMixin,
    LeaseMixin,
    ValidationMixin,
):
    """Application service enforcing workflow, context and role invariants."""

    def __init__(self, store: SQLiteStore) -> None:
        self.store = store
        self.git_worktrees = GitWorktreeManager(store.path.parent / "worktrees")
        self.project_tests = ProjectTestRunner(store.path.parent / "evidence")
        # Recovery patches stay outside the worktree root so no agent can reach
        # another agent's abandoned work through its own assigned worktree.
        self.recovery_root = store.path.parent / "recovery"
