"""The repository map an agent reads first must stay small and must not lie.

AGENTS.md grew into a release diary that named an installed version two
releases behind the host. These checks keep the entry router bounded and every
relative link in the agent-facing documents pointing at a file that exists.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
# Entry files every fresh agent session reads. Detail belongs in routed docs.
ENTRY_WORD_BUDGET = {"AGENTS.md": 900}


def _documents() -> list[Path]:
    paths = [ROOT / "AGENTS.md", ROOT / "README.md", ROOT / "deploy" / "README.md"]
    paths += sorted((ROOT / "docs").rglob("*.md"))
    paths += sorted((ROOT / "skills").rglob("*.md"))
    return [path for path in paths if path.is_file()]


def _relative_links(path: Path) -> list[str]:
    text = re.sub(r"```.*?```", "", path.read_text(encoding="utf-8"), flags=re.DOTALL)
    links = []
    for target in LINK.findall(text):
        if re.match(r"^[a-z][a-z0-9+.-]*:", target, re.IGNORECASE) or target.startswith("#"):
            continue
        links.append(target.split("#", 1)[0])
    return [link for link in links if link]


@pytest.mark.parametrize("document", _documents(), ids=lambda p: p.relative_to(ROOT).as_posix())
def test_relative_links_resolve(document: Path) -> None:
    missing = [
        link for link in _relative_links(document)
        if not (document.parent / link).resolve().exists()
    ]
    assert not missing, f"{document.relative_to(ROOT)} links to missing files: {missing}"


@pytest.mark.parametrize("name,budget", sorted(ENTRY_WORD_BUDGET.items()))
def test_entry_router_stays_within_budget(name: str, budget: int) -> None:
    words = len((ROOT / name).read_text(encoding="utf-8").split())
    assert words <= budget, (
        f"{name} has {words} words (budget {budget}). Move detail into a routed "
        "document and link it with a 'read when' trigger instead."
    )


def test_the_entry_router_records_no_live_numbers() -> None:
    """AGENTS.md said "1167 passed ... on 2026-10-05" and "about 5 minutes" while
    forbidding live state; a baseline comes from running unchanged main."""
    text = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    live = re.findall(
        r"\b\d+ (?:passed|skipped|failed)\b|\b20\d\d-\d\d-\d\d\b|\b\d+(?:\.\d+)? ?min(?:ute)?s?\b"
        r"|coverage \d+%",
        text,
    )
    assert not live, f"AGENTS.md records live numbers: {live}"
    assert "unchanged `main`" in text


def test_the_enforced_invariants_claim_no_priority_cap() -> None:
    """The server has no cap on Planner priorities; the rule lives in the Planner skill."""
    text = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    enforced = text.split("## Protocol invariants", 1)[1].split("\n## ", 1)[0]
    assert "The service enforces these" in enforced
    assert "priorit" not in enforced.lower()
    assert "at most three active priorities" in (
        ROOT / "skills" / "hoh-planner" / "SKILL.md"
    ).read_text(encoding="utf-8")


DATABASE_ASSIGNMENT = re.compile(r"HOH_DATABASE_PATH\s*=\s*['\"]?([^'\"\s`]+)")
# Databases that look plausible but are not the authoritative one (LOCAL_RUNTIME.md).
WRONG_DATABASES = ("d:/hoh-service/data/", "control_plane.db")


@pytest.mark.parametrize("document", _documents(), ids=lambda p: p.relative_to(ROOT).as_posix())
def test_no_document_points_a_component_at_a_wrong_database(document: Path) -> None:
    """RUNTIME_OPERATIONS.md once told operators to start a supervisor on D:/HoH-service/data."""
    wrong = []
    for value in DATABASE_ASSIGNMENT.findall(document.read_text(encoding="utf-8")):
        normalized = value.replace("\\", "/").lower()
        absolute = re.match(r"^([a-z]:/|/|<|\$)", normalized)
        if not absolute or any(marker in normalized for marker in WRONG_DATABASES):
            wrong.append(value)
    assert not wrong, f"{document.relative_to(ROOT)} sets HOH_DATABASE_PATH to {wrong}"


def test_runtime_operations_is_not_a_release_note() -> None:
    text = (ROOT / "docs" / "RUNTIME_OPERATIONS.md").read_text(encoding="utf-8")
    assert "This release" not in text
    assert not re.search(r"^#+ .*\b0\.\d+\.\d+", text, re.MULTILINE), "no version-scoped headings"


def test_every_doc_is_indexed() -> None:
    index = (ROOT / "docs" / "README.md").read_text(encoding="utf-8")
    linked = {link for link in _relative_links(ROOT / "docs" / "README.md")}
    unindexed = [
        path.relative_to(ROOT / "docs").as_posix()
        for path in sorted((ROOT / "docs").glob("*.md"))
        if path.name != "README.md" and path.relative_to(ROOT / "docs").as_posix() not in linked
    ]
    assert index and not unindexed, f"docs/README.md does not route to: {unindexed}"
