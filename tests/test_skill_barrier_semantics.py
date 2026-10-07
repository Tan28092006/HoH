"""The shared card describes barriers the way the server enforces them (wave 8D, A8).

Since 0.3.32 a reconciliation may freeze one ticket and its dependents, HoH's
own merge freezes nothing, and an outside commit freezes only the tickets it
reaches. The card's "Hard stops" still said every barrier froze the board, which
taught agents to idle while their ticket was free.
"""
from __future__ import annotations

import re
from pathlib import Path

CARD = Path(__file__).resolve().parents[1] / "skills" / "hoh-control-plane" / "SKILL.md"


def _hard_stops() -> str:
    text = CARD.read_text(encoding="utf-8")
    section = text.split("## Hard stops", 1)[1].split("\n## ", 1)[0]
    return re.sub(r"\s+", " ", section)


def test_a_barrier_freezes_only_the_tickets_it_names() -> None:
    section = _hard_stops()

    assert "only for the tickets it names" in section
    assert "every other ticket keeps flowing" in section
    assert "`planning_barrier` lists only what applies to its ticket" in section


def test_each_barrier_source_is_described_with_its_reach() -> None:
    section = _hard_stops()

    assert "QA rejection, integration regression or drift" in section
    assert "freezes that ticket and its dependents" in section
    assert "outside commit freezes only the tickets its paths reach" in section
    assert "docs/spec, root files, rewritten history or an unknown diff" in section
    assert "HoH's own merge freezes nothing" in section
    assert "`planning_base_stale`" in section


def test_the_card_no_longer_says_every_barrier_freezes_the_board() -> None:
    section = _hard_stops()

    assert "merges until Planner resolves the observation" not in section
