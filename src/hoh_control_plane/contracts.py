"""One schema for the agent writes that carry structured payloads.

The MCP tool signatures (which FastMCP turns into each tool's JSON input schema)
and the REST request models are both built from these types, and the service
checks the same constants, so a limit or an enum cannot drift between the
transports and the rule that enforces it. Each constant names the service check
it mirrors.
"""
from typing import Annotated, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

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

Verdict = Literal["passed", "failed"]
FindingStatus = Literal["verified", "failed", "insufficient_evidence", "flaky"]
ValidationMethod = Literal["black_box", "white_box", "mixed"]

VERDICTS: frozenset[str] = frozenset(get_args(Verdict))
FINDING_STATUSES: frozenset[str] = frozenset(get_args(FindingStatus))
VALIDATION_METHODS: frozenset[str] = frozenset(get_args(ValidationMethod))


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
DeveloperClaims = Annotated[
    list[DeveloperClaim], Field(max_length=MAX_DEVELOPER_CLAIMS)
]


def as_payload(value: object) -> object:
    """Plain JSON for the service: models become dicts of the fields the caller set."""
    if isinstance(value, BaseModel):
        return value.model_dump(exclude_unset=True)
    if isinstance(value, list):
        return [as_payload(item) for item in value]
    return value
