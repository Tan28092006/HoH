"""Re-reading only the part of the plan that moved."""
from __future__ import annotations

import json

import pytest

from hoh_control_plane.domain import Actor, AuthorizationError, Role, ValidationError


DOCUMENT = "HOH_MULTI_AGENT_CONTROL_PLANE.md"

SPEC = """# Control Plane

Preamble text.

## 1. Roles

A Planner plans. A Developer implements.

## 2. Context packets

A packet is bounded by a token budget.

### 2.1 Compaction

Least important last.

## 3. Leases

A lease is exclusive.
"""


def _sync(service, actor, text: str, document: str = DOCUMENT):
    return service.sync_spec(actor, "project-1", document, text)


def _digest(service, actor, after: int = 0, **kwargs):
    return service.spec_digest(actor, "project-1", after, **kwargs)


def _headings(digest) -> list[str]:
    return [section["heading"] for section in digest["sections"]]


def _tokens(payload) -> int:
    return len(json.dumps(payload, default=str)) // 4


# --- the cold read ---------------------------------------------------------------


def test_a_cold_agent_gets_the_table_of_contents(service, actors, seeded) -> None:
    planner = actors["planner"]
    _sync(service, planner, SPEC)

    digest = _digest(service, planner)

    assert "Control Plane > 2. Context packets" in _headings(digest)
    assert "Control Plane > 2. Context packets > 2.1 Compaction" in _headings(digest)


def test_a_section_says_where_to_open_and_what_it_costs(service, actors, seeded) -> None:
    """The point is to read two sections, not the document."""
    planner = actors["planner"]
    _sync(service, planner, SPEC)

    section = [
        entry for entry in _digest(service, planner)["sections"]
        if entry["heading"].endswith("3. Leases")
    ][0]

    start, end = section["lines"]
    assert SPEC.splitlines()[start - 1] == "## 3. Leases"
    assert end >= start
    assert section["tokens"] > 0


def test_the_digest_carries_no_prose(service, actors, seeded) -> None:
    """The document stays the source of truth in git; this is only the index."""
    planner = actors["planner"]
    _sync(service, planner, SPEC)

    rendered = json.dumps(_digest(service, planner))

    assert "A lease is exclusive" not in rendered
    assert "Least important last" not in rendered


# --- the warm read ---------------------------------------------------------------


def test_an_unchanged_document_costs_almost_nothing(service, actors, seeded) -> None:
    """The common case, so it has to be the cheap one."""
    planner = actors["planner"]
    first = _sync(service, planner, SPEC)

    quiet = _digest(service, planner, first["revision"])

    assert quiet["sections"] == []
    assert _tokens(quiet) < 40


def test_only_the_changed_section_comes_back(service, actors, seeded) -> None:
    planner = actors["planner"]
    revision = _sync(service, planner, SPEC)["revision"]
    edited = SPEC.replace("A lease is exclusive.", "A lease is exclusive and expires.")

    _sync(service, planner, edited)

    assert _headings(_digest(service, planner, revision)) == ["Control Plane > 3. Leases"]


def test_re_syncing_an_unchanged_document_changes_nothing(service, actors, seeded) -> None:
    """A no-op sync must not look like a change to anyone holding a cursor."""
    planner = actors["planner"]
    revision = _sync(service, planner, SPEC)["revision"]

    again = _sync(service, planner, SPEC)

    assert again["revision"] == revision
    assert (again["added"], again["changed"], again["removed"]) == ([], [], [])
    assert _digest(service, planner, revision)["sections"] == []


def test_moving_a_section_without_editing_it_is_not_a_change(service, actors, seeded) -> None:
    """Otherwise every insertion above it wakes a Planner to re-read all of it."""
    planner = actors["planner"]
    revision = _sync(service, planner, SPEC)["revision"]
    shifted = SPEC.replace("Preamble text.", "Preamble text.\n\nAn added paragraph.")

    _sync(service, planner, shifted)

    assert "Control Plane > 3. Leases" not in _headings(_digest(service, planner, revision))


def test_a_moved_section_still_points_at_the_right_lines(service, actors, seeded) -> None:
    """An unchanged hash must not leave a reader opening the wrong lines."""
    planner = actors["planner"]
    _sync(service, planner, SPEC)
    shifted = SPEC.replace("Preamble text.", "Preamble text.\n\nAn added paragraph.")
    _sync(service, planner, shifted)

    section = [
        entry for entry in _digest(service, planner)["sections"]
        if entry["heading"].endswith("3. Leases")
    ][0]

    assert shifted.splitlines()[section["lines"][0] - 1] == "## 3. Leases"


def test_a_new_section_is_reported_as_added(service, actors, seeded) -> None:
    planner = actors["planner"]
    revision = _sync(service, planner, SPEC)["revision"]

    result = _sync(service, planner, SPEC + "\n## 4. Evidence\n\nEvidence is durable.\n")

    assert result["added"] == ["Control Plane > 4. Evidence"]
    assert _headings(_digest(service, planner, revision)) == ["Control Plane > 4. Evidence"]


def test_a_removed_section_is_news(service, actors, seeded) -> None:
    """A Planner still planning against it is planning against something gone."""
    planner = actors["planner"]
    revision = _sync(service, planner, SPEC)["revision"]
    without_leases = SPEC.split("## 3. Leases")[0]

    result = _sync(service, planner, without_leases)

    assert result["removed"] == ["Control Plane > 3. Leases"]
    gone = _digest(service, planner, revision)["sections"]
    assert gone[0]["status"] == "removed"


# --- the parser has to be right ---------------------------------------------------


def test_a_heading_inside_a_code_fence_is_not_a_section(service, actors, seeded) -> None:
    """One miscounted fence shifts every section after it."""
    planner = actors["planner"]
    fenced = SPEC + "\n## 4. Examples\n\n```sh\n# 5. Not a heading\nrun --it\n```\n"

    _sync(service, planner, fenced)

    assert "Control Plane > 5. Not a heading" not in _headings(_digest(service, planner))
    assert "Control Plane > 4. Examples" in _headings(_digest(service, planner))


def test_two_sections_with_the_same_name_stay_separate(service, actors, seeded) -> None:
    """Collapsing them into one row would hide a real change in the second."""
    planner = actors["planner"]
    repeated = "# Doc\n\n## Notes\n\nFirst.\n\n## Notes\n\nSecond.\n"

    _sync(service, planner, repeated)

    assert len(_digest(service, planner)["sections"]) == 3


def test_editing_one_of_two_like_named_sections_reports_only_that_one(
    service, actors, seeded
) -> None:
    planner = actors["planner"]
    repeated = "# Doc\n\n## Notes\n\nFirst.\n\n## Notes\n\nSecond.\n"
    revision = _sync(service, planner, repeated)["revision"]

    _sync(service, planner, repeated.replace("Second.", "Second, revised."))

    assert _headings(_digest(service, planner, revision)) == ["Doc > Notes #2"]


def test_nesting_is_reflected_in_the_heading_path(service, actors, seeded) -> None:
    planner = actors["planner"]
    _sync(service, planner, SPEC)

    assert "Control Plane > 2. Context packets > 2.1 Compaction" in _headings(
        _digest(service, planner)
    )


# --- bounds and access ------------------------------------------------------------


def test_a_cursor_this_history_cannot_have_issued_asks_for_a_reload(
    service, actors, seeded
) -> None:
    planner = actors["planner"]
    _sync(service, planner, SPEC)

    digest = _digest(service, planner, 999)

    assert digest["reset"] is True
    assert digest["sections"] == []


def test_the_page_is_bounded_and_says_when_more_remains(service, actors, seeded) -> None:
    planner = actors["planner"]
    _sync(service, planner, SPEC)

    digest = _digest(service, planner, 0, limit=2)

    assert len(digest["sections"]) == 2
    assert digest["has_more"] is True


def test_a_negative_cursor_is_refused(service, actors, seeded) -> None:
    with pytest.raises(ValidationError):
        _digest(service, actors["planner"], -1)


def test_only_a_planner_may_say_what_the_plan_is(service, actors, seeded) -> None:
    """The spec is the plan of record; an execution role must not rewrite its index."""
    with pytest.raises(AuthorizationError):
        _sync(service, actors["developer"], SPEC)
    with pytest.raises(AuthorizationError):
        _sync(service, actors["qa"], SPEC)


def test_any_role_may_read_what_changed(service, actors, seeded) -> None:
    planner = actors["planner"]
    _sync(service, planner, SPEC)

    for role in ("developer", "qa", "integrator"):
        assert _digest(service, actors[role])["sections"]


def test_the_index_is_scoped_to_the_actor_s_projects(service, actors, seeded) -> None:
    outsider = Actor("outsider", Role.PLANNER, frozenset({"elsewhere"}))

    with pytest.raises(AuthorizationError):
        service.spec_digest(outsider, "project-1", 0)
    with pytest.raises(AuthorizationError):
        service.sync_spec(outsider, "project-1", DOCUMENT, SPEC)


def test_two_documents_do_not_collide(service, actors, seeded) -> None:
    planner = actors["planner"]
    _sync(service, planner, SPEC)

    result = _sync(service, planner, SPEC, document="OTHER.md")

    assert result["removed"] == [], "syncing one document does not retire another's sections"
    sections = _digest(service, planner, 0, limit=200)["sections"]
    assert len(sections) == 10, "five sections indexed under each document"
    assert {section["document"] for section in sections} == {DOCUMENT, "OTHER.md"}


# --- the outline a cold agent can afford ------------------------------------------


def test_the_outline_can_be_trimmed_to_its_top_levels(service, actors, seeded) -> None:
    """106 sections costing half the document is no saving at all."""
    planner = actors["planner"]
    _sync(service, planner, SPEC)

    shallow = _digest(service, planner, 0, max_depth=2)

    assert "Control Plane > 2. Context packets > 2.1 Compaction" not in _headings(shallow)
    assert "Control Plane > 2. Context packets" in _headings(shallow)
    assert _tokens(shallow) < _tokens(_digest(service, planner))


def test_a_trimmed_entry_covers_the_sections_it_hides(service, actors, seeded) -> None:
    """Otherwise it sends a reader to the preamble and calls it the section."""
    planner = actors["planner"]
    _sync(service, planner, SPEC)

    trimmed = [
        entry for entry in _digest(service, planner, 0, max_depth=2)["sections"]
        if entry["heading"].endswith("2. Context packets")
    ][0]
    full = [
        entry for entry in _digest(service, planner)["sections"]
        if entry["heading"].endswith("2. Context packets")
    ][0]

    start, end = trimmed["lines"]
    assert "Least important last." in "\n".join(SPEC.splitlines()[start - 1 : end])
    assert trimmed["tokens"] > full["tokens"], "the nested section counts toward it"


def test_asking_what_changed_is_not_trimmed_by_default(service, actors, seeded) -> None:
    """Precision is the point there; a nested edit must not be silently dropped."""
    planner = actors["planner"]
    revision = _sync(service, planner, SPEC)["revision"]

    _sync(service, planner, SPEC.replace("Least important last.", "Least important first."))

    assert _headings(_digest(service, planner, revision)) == [
        "Control Plane > 2. Context packets > 2.1 Compaction"
    ]


def test_a_depth_outside_the_heading_range_is_refused(service, actors, seeded) -> None:
    with pytest.raises(ValidationError):
        _digest(service, actors["planner"], 0, max_depth=0)
    with pytest.raises(ValidationError):
        _digest(service, actors["planner"], 0, max_depth=7)


def test_derived_metadata_is_refreshed_even_when_the_text_did_not_change(
    service, actors, seeded
) -> None:
    """A stale span or size outlives the edit that invalidated it otherwise."""
    planner = actors["planner"]
    _sync(service, planner, SPEC)
    with service.store.connection(immediate=True) as connection:
        connection.execute("UPDATE spec_sections SET token_estimate = 1, end_line = 1")

    revision = service.spec_digest(planner, "project-1", 0)["revision"]
    _sync(service, planner, SPEC)

    section = [
        entry for entry in _digest(service, planner)["sections"]
        if entry["heading"].endswith("3. Leases")
    ][0]
    assert section["tokens"] > 1, "the size was recomputed"
    assert section["lines"][1] > 1, "and so was the span"
    assert _digest(service, planner, revision)["sections"] == [], "without looking like a change"


def test_a_subtree_extent_is_folded_up_in_one_pass(service, actors, seeded) -> None:
    """The obvious nested scan is quadratic, and specifications only ever grow."""
    from hoh_control_plane.service.spec import SpecMixin

    deep = [
        {"depth": 1, "end_line": 10, "token_estimate": 1},
        {"depth": 2, "end_line": 20, "token_estimate": 2},
        {"depth": 3, "end_line": 30, "token_estimate": 4},
        {"depth": 2, "end_line": 40, "token_estimate": 8},
        {"depth": 1, "end_line": 50, "token_estimate": 16},
    ]

    extents = SpecMixin._subtree_extents(deep)

    assert extents[0] == (40, 15), "the first root covers its three descendants"
    assert extents[1] == (30, 6), "and the branch covers only its own child"
    assert extents[3] == (40, 8), "a leaf covers itself"
    assert extents[4] == (50, 16), "the last root has no descendants to absorb"


def test_the_outline_does_not_read_sections_it_is_not_returning(
    service, actors, seeded
) -> None:
    """Trimming has to cost a page, not a walk over every section in the document."""
    planner = actors["planner"]
    document = "# Root\n\n" + "".join(
        f"## Section {index}\n\nBody.\n\n### Nested {index}\n\nDeep.\n\n" for index in range(60)
    )
    _sync(service, planner, document)

    with service.store.connection() as connection:
        plan = "\n".join(
            str(row[3])
            for row in connection.execute(
                """
                EXPLAIN QUERY PLAN
                SELECT * FROM spec_sections
                WHERE project_id = ? AND changed_at_revision > ? AND depth <= ?
                ORDER BY document ASC, start_line ASC LIMIT ?
                """,
                ("project-1", 0, 2, 51),
            ).fetchall()
        )

    assert "spec_sections_cursor" in plan or "SEARCH" in plan, (
        f"the outline scans the whole index:\n{plan}"
    )
    trimmed = _digest(service, planner, 0, limit=200, max_depth=2)
    assert len(trimmed["sections"]) == 61, "root plus 60 second-level sections"
    assert all(entry["tokens"] > 0 for entry in trimmed["sections"])
