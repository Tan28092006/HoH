"""One schema for the agent writes that carry structured payloads.

The MCP tool signatures (which FastMCP turns into each tool's JSON input schema)
and the REST request models are both built from these types, and the service
checks the same constants, so a limit or an enum cannot drift between the
transports and the rule that enforces it. Each constant names the service check
it mirrors.
"""
from typing import Annotated, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, StringConstraints


class AutonomyLimits(BaseModel):
    """Hard admission ceilings; tokens are an observed-usage threshold."""

    model_config = ConfigDict(extra="forbid", strict=True)
    wall_seconds: int = Field(ge=30, le=604800)
    launch_attempts: int = Field(ge=1, le=10000)
    in_flight: int = Field(ge=1, le=20)
    suite_starts: int = Field(ge=0, le=10000)
    landed_tickets: int = Field(ge=0, le=10000)
    crash_retries: int = Field(default=1, ge=0, le=10)
    observed_tokens: int | None = Field(default=None, ge=1, le=2000000000)

# --- Validation contracts (service.project_policy._validated_validation_contract)
# The ticket section is never compacted, so contract text is carried by every
# packet for the ticket's whole life (release 0.3.21).
CONTRACT_TEXT_CHARS = 600
CONTRACT_TOKEN_BUDGET = 1_200
CONTRACT_MAX_SCENARIOS = 30
SCENARIO_REF_CHARS = 200
EVIDENCE_TYPE_CHARS = 50
CONTRACT_MAX_EVIDENCE_TYPES = 20
CAPABILITY_CHARS = 100
CONTRACT_MAX_CAPABILITIES = 20

# --- QA verdicts and findings (service.qa.submit_qa_verdict)
MAX_FINDINGS = 50
FINDING_CRITERION_CHARS = 2_000
OBSERVATION_CHARS = 8_000
SEVERITY_CHARS = 30
MAX_EVIDENCE_PER_FINDING = 30
STORAGE_REF_CHARS = 1_000
EVIDENCE_SCENARIO_REF_CHARS = 1_000
DIGEST_CHARS = 128

# --- Candidate submission (service.candidates.submit_candidate)
MAX_DEVELOPER_CLAIMS = 50
DEVELOPER_CLAIM_CHARS = 2_000

# --- Repository reconciliation decisions
# (service.repository_reconciliation.resolve_repository_reconciliation)
MAX_TICKET_DECISIONS = 500
DECISION_REASON_CHARS = 2_000
DECISION_TICKET_ID_CHARS = 200

# --- Worker usage (service.usage.report_usage)
# How a ticket was worked: by a fresh subagent the dispatcher started for it, by
# the waiting dispatcher chat itself, or in a new session started for it.
MAX_REPORTED_TOKENS = 100_000_000

Verdict = Literal["passed", "failed"]
FindingStatus = Literal["verified", "failed", "insufficient_evidence", "flaky"]
ValidationMethod = Literal["black_box", "white_box", "mixed"]
WorkerKind = Literal["fresh_subagent", "dispatcher", "new_session"]
#: What Planner decided for one ticket that was open when a commit was observed.
Disposition = Literal["keep", "revise", "cancel", "supersede", "complete", "rework"]

VERDICTS: frozenset[str] = frozenset(get_args(Verdict))
FINDING_STATUSES: frozenset[str] = frozenset(get_args(FindingStatus))
VALIDATION_METHODS: frozenset[str] = frozenset(get_args(ValidationMethod))
WORKER_KINDS: frozenset[str] = frozenset(get_args(WorkerKind))
DISPOSITIONS: frozenset[str] = frozenset(get_args(Disposition))
#: The kinds that keep one ticket's transcript out of the next ticket.
FRESH_WORKER_KINDS: frozenset[str] = frozenset({"fresh_subagent", "new_session"})

# --- Enumerated tool parameters. Each set is the one the named service check
# compares against, so the MCP schema offers exactly the values it accepts.
#: The roles that execute tickets (ticket_authoring.create_ticket required_role,
#: planner_jobs.resume_ticket_after_failure resume_role, discovery awaiting_role).
ExecutionRole = Literal["developer", "qa", "integrator"]
#: planner_jobs.report_execution_failure failure_class.
FailureClass = Literal[
    "source_or_contract", "infrastructure", "dependency", "integration_conflict",
    "security_or_data", "unknown_durable",
]
#: The failed worker job a failure report may cite (report_execution_failure).
FailedJobKind = Literal["worktree_provision", "candidate_submission", "qa_test", "merge"]
#: The queued job a wait may follow (event_wait.JOBS).
WaitJobKind = Literal["provision", "submission", "qa", "merge"]
#: ticket_authoring.create_ticket ticket_type; revert tickets are also opened by
#: the server when main goes red (run_guardrails).
TicketType = Literal[
    "feature_work", "bug", "qa_validation", "investigation", "integration",
    "infrastructure", "revert",
]
#: ticket_retirement.retire_ticket_for_reconciliation target_status.
RetirementStatus = Literal["CANCELLED", "SUPERSEDED"]
#: The context entry types a Planner writes (planner_context.append_planner_context);
#: the rest of the vocabulary is written by the control plane itself.
PlannerEntryType = Literal["planner_decision", "design_decision", "risk", "note"]
#: repository_reconciliation.list_repository_reconciliations status.
ReconciliationStatus = Literal["PENDING", "RESOLVED"]
#: project_setup.configure_repository workspace_model (domain.WorkspaceModel).
WorkspaceModelName = Literal["colocated", "agent_workspace"]
#: project_policy.configure_verification_profile quality_policy.
QualityPolicy = Literal["legacy", "claim-evidence-v1"]
#: discovery status filter (domain.TicketStatus).
TicketStatusName = Literal[
    "BACKLOG", "READY", "DEV_IN_PROGRESS", "QA_READY", "QA_IN_PROGRESS", "REWORK",
    "VERIFIED", "MERGE_READY", "DONE", "BLOCKED", "CANCELLED", "SUPERSEDED",
]

EXECUTION_ROLES: frozenset[str] = frozenset(get_args(ExecutionRole))
FAILURE_CLASSES: frozenset[str] = frozenset(get_args(FailureClass))
FAILED_JOB_KINDS: frozenset[str] = frozenset(get_args(FailedJobKind))
WAIT_JOB_KINDS: frozenset[str] = frozenset(get_args(WaitJobKind))
TICKET_TYPES: frozenset[str] = frozenset(get_args(TicketType))
RETIREMENT_STATUSES: frozenset[str] = frozenset(get_args(RetirementStatus))
PLANNER_ENTRY_TYPES: frozenset[str] = frozenset(get_args(PlannerEntryType))
RECONCILIATION_STATUSES: frozenset[str] = frozenset(get_args(ReconciliationStatus))
WORKSPACE_MODELS: frozenset[str] = frozenset(get_args(WorkspaceModelName))
QUALITY_POLICIES: frozenset[str] = frozenset(get_args(QualityPolicy))
TICKET_STATUSES: frozenset[str] = frozenset(get_args(TicketStatusName))


def _text(max_length: int, min_length: int = 0) -> object:
    """A bounded string type; the bound appears in the generated JSON schema."""
    return Annotated[str, StringConstraints(min_length=min_length, max_length=max_length)]


ContractText = _text(CONTRACT_TEXT_CHARS, 1)
ScenarioRef = _text(SCENARIO_REF_CHARS, 1)
EvidenceType = _text(EVIDENCE_TYPE_CHARS, 1)
Capability = _text(CAPABILITY_CHARS, 1)
Observation = _text(OBSERVATION_CHARS, 1)
StorageRef = _text(STORAGE_REF_CHARS, 1)
EvidenceScenarioRef = _text(EVIDENCE_SCENARIO_REF_CHARS, 1)
Digest = _text(DIGEST_CHARS, 1)
FindingCriterion = _text(FINDING_CRITERION_CHARS, 1)
Severity = _text(SEVERITY_CHARS, 1)
DeveloperClaim = _text(DEVELOPER_CLAIM_CHARS, 1)
TokenCount = Annotated[int, Field(ge=0, le=MAX_REPORTED_TOKENS)]


class ContractModel(BaseModel):
    """Unknown fields are refused, so a misspelt key fails instead of vanishing."""

    model_config = ConfigDict(extra="forbid")


class ValidationScenarioInput(ContractModel):
    """One acceptance criterion bound to a reproducible QA observation."""

    criterion: ContractText
    scenario_ref: ScenarioRef
    method: ValidationMethod
    expected_observation: ContractText
    required_evidence_types: list[EvidenceType] = Field(
        min_length=1, max_length=CONTRACT_MAX_EVIDENCE_TYPES
    )
    required_capabilities: list[Capability] = Field(
        default_factory=list, max_length=CONTRACT_MAX_CAPABILITIES
    )


ValidationContract = Annotated[
    list[ValidationScenarioInput], Field(min_length=1, max_length=CONTRACT_MAX_SCENARIOS)
]


class EvidenceInput(ContractModel):
    """One observation, and where its artifact is stored."""

    type: EvidenceType
    observation: Observation
    storage_ref: StorageRef
    scenario_ref: EvidenceScenarioRef | None = None
    environment_digest: Digest | None = None
    storage_checksum: Digest | None = None


class FindingInput(ContractModel):
    """The result for one acceptance criterion, with its evidence."""

    criterion: FindingCriterion
    status: FindingStatus
    severity: Severity = "unknown"
    observation: Observation
    evidence: list[EvidenceInput] = Field(min_length=1, max_length=MAX_EVIDENCE_PER_FINDING)


Findings = Annotated[list[FindingInput], Field(min_length=1, max_length=MAX_FINDINGS)]


DecisionTicketId = _text(DECISION_TICKET_ID_CHARS, 1)
DecisionReason = _text(DECISION_REASON_CHARS, 1)


class TicketDecision(ContractModel):
    """Planner's classification of one ticket that was open when a commit was observed.

    keep: the plan still holds. revise: the ticket was updated (its version
    moved). cancel / supersede / complete: the ticket is now CANCELLED,
    SUPERSEDED (name replacement_ticket_id) or DONE. rework: the ticket moved
    to REWORK or BLOCKED after the observation. The service checks each disposition against the
    board, so apply the ticket change first, then record the decision.
    """

    ticket_id: DecisionTicketId
    disposition: Disposition
    reason: DecisionReason
    replacement_ticket_id: DecisionTicketId | None = None


TicketDecisions = Annotated[list[TicketDecision], Field(max_length=MAX_TICKET_DECISIONS)]
DeveloperClaims = Annotated[
    list[DeveloperClaim], Field(max_length=MAX_DEVELOPER_CLAIMS)
]

# --- Skill manifest (service.discovery._validate_skill_manifest)
MAX_SKILL_MANIFEST_ENTRIES = 30
SkillName = _text(100, 1)
SkillVersion = _text(50, 1)
SkillChecksum = _text(128, 1)
SkillLoadedAt = _text(40, 1)


class SkillManifestEntry(ContractModel):
    """One skill file this agent loaded: provenance only, never an authorization input.

    loaded_at is an ISO-8601 timestamp such as 2026-10-06T09:00:00Z.
    """

    name: SkillName
    version: SkillVersion
    checksum: SkillChecksum
    loaded_at: SkillLoadedAt


SkillManifest = Annotated[
    list[SkillManifestEntry], Field(max_length=MAX_SKILL_MANIFEST_ENTRIES)
]


def as_payload(value: object) -> object:
    """Plain JSON for the service: models become dicts of the fields the caller set."""
    if isinstance(value, BaseModel):
        return value.model_dump(exclude_unset=True)
    if isinstance(value, list):
        return [as_payload(item) for item in value]
    return value
