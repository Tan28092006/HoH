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


def _indexed_from(index: Path) -> set[str]:
    """Documents an index links, as paths relative to docs/."""
    docs = (ROOT / "docs").resolve()
    found = set()
    for link in _relative_links(index):
        target = (index.parent / link).resolve()
        if target.is_relative_to(docs):
            found.add(target.relative_to(docs).as_posix())
    return found


def test_every_doc_is_indexed() -> None:
    """Every document under docs/, at any depth, is reachable from docs/README.md.

    A nested document counts when docs/README.md links it directly, or links
    the README.md of its own directory and that README links it (releases/).
    rglob, not glob: reviews/ and releases/ are documents too (review E6).
    """
    index = ROOT / "docs" / "README.md"
    linked = _indexed_from(index)
    unindexed = []
    for path in sorted((ROOT / "docs").rglob("*.md")):
        relative = path.relative_to(ROOT / "docs").as_posix()
        if path == index or relative in linked:
            continue
        local_index = path.parent / "README.md"
        local_relative = local_index.relative_to(ROOT / "docs").as_posix()
        if (
            path != local_index and local_index.is_file() and local_relative in linked
            and relative in _indexed_from(local_index)
        ):
            continue
        unindexed.append(relative)
    assert index.read_text(encoding="utf-8") and not unindexed, (
        f"docs/README.md does not route to: {unindexed}"
    )
