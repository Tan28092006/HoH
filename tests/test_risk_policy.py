from hoh_control_plane.local_agent import model_family_of
from hoh_control_plane.service import ControlPlaneService
from hoh_control_plane.service.risk_policy import (
    DEFAULT_HIGH_RISK_PATTERNS,
    DEFAULT_MEDIUM_RISK_PATTERNS,
)

POLICY = {
    "checker_distinct_from_maker": True,
    "high_risk_patterns": list(DEFAULT_HIGH_RISK_PATTERNS),
    "medium_risk_patterns": list(DEFAULT_MEDIUM_RISK_PATTERNS),
    "medium_max_files": 3,
}


def _classify(changed, scope=()):
    service = object.__new__(ControlPlaneService)
    return service._classify_risk(POLICY, changed, list(scope))["tier"]


def test_risk_tiers_from_changed_paths():
    assert _classify(["src/hoh_control_plane/store_schema.py"]) == "high"
    assert _classify(["skills/hoh-qa/SKILL.md"]) == "high"
    assert _classify(["src/hoh_control_plane/domain.py"]) == "medium"
    assert _classify(["src/a.py", "src/b.py", "src/c.py", "src/d.py"]) == "medium"
    assert _classify(["src/a.py"]) == "low"


def test_risk_tier_from_scope_before_a_candidate():
    assert _classify(None, ["skills/**"]) == "high"
    assert _classify(None, ["src/app/**"]) == "low"


def test_model_family_is_derived_from_the_model_id():
    assert model_family_of("claude-opus-5-5") == "anthropic"
    assert model_family_of("gpt-5") == "openai"
    assert model_family_of("gemini-3-pro") == "google"
    assert model_family_of("llama-4") == "other"
    assert model_family_of(None) is None
