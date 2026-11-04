"""Which practice skills each role loads is a table: pin it, and the boundary it implies.

The table decides what every worker loads (and therefore what a ticket costs), and
skill_plan.py is one of the project's own high-risk paths, so a changed cell has to
show up as a changed test.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from hoh_control_plane.skill_plan import practice_skills_for, review_delegation_for

SKILLS = Path(__file__).resolve().parents[1] / "skills"

PRACTICE_SKILLS = {
    ("planner", "low"): ["workflow"],
    ("planner", "medium"): ["workflow"],
    ("planner", "high"): ["workflow"],
    ("developer", "low"): ["testing"],
    ("developer", "medium"): ["workflow", "testing"],
    ("developer", "high"): ["workflow", "testing"],
    ("qa", "low"): ["testing"],
    ("qa", "medium"): ["testing", "ponytail-review"],
    ("qa", "high"): ["testing", "ponytail-review", "thermo-nuclear-code-quality-review", "pstack-interrogate"],
    ("integrator", "low"): [],
    ("integrator", "medium"): [],
    ("integrator", "high"): [],
    ("admin", "high"): [],
}


@pytest.mark.parametrize("role,tier", sorted(PRACTICE_SKILLS))
def test_each_role_and_tier_loads_exactly_its_practice_skills(role, tier):
    assert practice_skills_for(role, tier) == PRACTICE_SKILLS[(role, tier)]


def test_a_heavier_tier_never_loads_fewer_skills_than_a_lighter_one():
    for role in ("planner", "developer", "qa"):
        low, medium, high = (set(PRACTICE_SKILLS[(role, tier)]) for tier in ("low", "medium", "high"))
        assert low <= medium <= high, role


@pytest.mark.parametrize("reviewers,expected", [(0, None), (3, 3)])
def test_only_a_plan_with_advisory_reviewers_delegates_a_review(reviewers, expected):
    delegation = review_delegation_for({"advisory_reviewers_per_owner": reviewers})
    if expected is None:
        assert delegation is None
        return
    assert delegation == {"skill": "pstack-interrogate", "reviewers": expected, "read_only": True,
                          "recursive": False, "wait_for_all": True, "run_full_suite": False}


def test_no_plan_means_no_delegation():
    assert review_delegation_for(None) is None
    assert review_delegation_for({}) is None


def test_every_practice_skill_a_role_loads_is_bounded_by_its_card():
    """A practice skill adds method; it never widens a role's authority (card text).

    Planner and QA already say so; Developer loads `workflow` at medium and high risk,
    and `workflow` also describes QA, integration and deployment stages.
    """
    card = (SKILLS / "hoh-developer" / "SKILL.md").read_text(encoding="utf-8")
    assert "practice skill" in card
    assert "never widen" in card
    for stage in ("QA", "integration", "deployment"):
        assert stage in card, f"Developer card does not say the {stage} stage is out of scope"
    qa_reference = (SKILLS / "hoh-qa" / "references" / "risk-review.md").read_text(encoding="utf-8")
    assert "do not grant those rights" in qa_reference
    planner_reference = (SKILLS / "hoh-planner" / "references" / "technical-planning.md").read_text(encoding="utf-8")
    assert "not applicable to the Planner" in planner_reference
