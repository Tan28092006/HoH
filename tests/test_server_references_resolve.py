"""Every reference the server hands an agent resolves through context.resolve (wave 8D, A2).

Planner jobs cite candidate:<id>, context:<id> and finding:<id>; packets surface
experience:<id>; events name ticket:<id>. context.resolve used to accept only
ctx: forms, so "inspect the linked findings" failed exactly when Planner had to
decide on rework. These tests drive one QA rejection through the MCP tools,
collect every reference from every result, and resolve each one.
"""
from __future__ import annotations

import re
from typing import Any

import pytest
from aci_helpers import drive_qa_rejection
from mcp.server.fastmcp.exceptions import ToolError

from hoh_control_plane.domain import Actor, AuthorizationError, NotFoundError, Role, ValidationError
from hoh_control_plane.mcp_server import create_mcp_server
from hoh_control_plane.service.context import BARE_REFERENCE_KINDS

# A field that carries a reference, by name; and the shape of a server reference
# (an agent's evidence storage_ref such as evidence://... is not one).
_REFERENCE_FIELD = re.compile(r"(^|_)(ref|refs|reference|references)$")
_REFERENCE_VALUE = re.compile(r"^[a-z_]+:[A-Za-z0-9_.\-]+(?::[A-Za-z0-9_.\-]+)*$")


def _references(value: Any, field: str = "") -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for name, item in value.items():
            found |= _references(item, name)
    elif isinstance(value, list):
        for item in value:
            found |= _references(item, field)
    elif (
        isinstance(value, str)
        and _REFERENCE_FIELD.search(field)
        and _REFERENCE_VALUE.match(value)
    ):
        found.add(value)
    return found


@pytest.mark.anyio
async def test_every_reference_in_a_qa_rejection_resolves_for_the_role_that_saw_it(
    service, actors, seeded
) -> None:
    results = await drive_qa_rejection(service, actors, seeded)
    by_role: dict[str, set[str]] = {}
    for role, _tool, result in results:
        by_role.setdefault(role, set()).update(_references(result))
    seen = set().union(*by_role.values())

    # The rejection's own citations are in the set, so the test is not vacuous.
    prefixes = {reference.split(":", 1)[0] for reference in seen}
    assert {"candidate", "context", "finding", "experience", "ctx", "ticket"} <= prefixes

    unresolved: dict[str, str] = {}
    for role, references in sorted(by_role.items()):
        server = create_mcp_server(service, actors[role])
        for reference in sorted(references):
            try:
                _content, resolved = await server.call_tool(
                    "context.resolve", {"reference": reference}
                )
            except ToolError as error:
                unresolved[f"{role} {reference}"] = str(error)
                continue
            assert resolved, reference
    assert unresolved == {}


def _rejected_candidate(results) -> tuple[str, list[str]]:
    for _role, tool, result in results:
        if tool == "qa.submit_verdict":
            job = result["planner_job"]
            return job["candidate_id"], job["evidence_refs"]
    raise AssertionError("no verdict in the drive")


@pytest.mark.anyio
async def test_the_planner_job_citations_resolve_to_what_they_name(
    service, actors, seeded
) -> None:
    results = await drive_qa_rejection(service, actors, seeded)
    candidate_id, evidence_refs = _rejected_candidate(results)
    planner = actors["planner"]

    candidate = service.resolve_context_reference(planner, f"candidate:{candidate_id}")
    assert candidate["reference"] == f"ctx:candidate:{candidate_id}"
    assert candidate["candidate"]["ticket_id"] == seeded["ticket"]["ticket_id"]
    assert {item["status"] for item in candidate["findings"]} == {"failed"}
    for section in candidate["sections"]:
        assert service.resolve_context_reference(planner, section)

    finding_refs = [ref for ref in evidence_refs if ref.startswith("finding:")]
    assert finding_refs
    for reference in finding_refs:
        finding = service.resolve_context_reference(planner, reference)["finding"]
        assert finding["candidate_id"] == candidate_id
        # The bare and ctx: spellings name the same thing.
        ctx_form = "ctx:finding:" + reference.split(":", 1)[1]
        assert service.resolve_context_reference(planner, ctx_form)["finding"] == finding

    [context_ref] = [ref for ref in evidence_refs if ref.startswith("context:")]
    entry = service.resolve_context_reference(planner, context_ref)["entry"]
    assert entry["entry_id"] == context_ref.split(":", 1)[1]


@pytest.mark.anyio
async def test_a_candidate_named_by_its_commit_resolves(service, actors, seeded) -> None:
    results = await drive_qa_rejection(service, actors, seeded)
    candidate_id, _refs = _rejected_candidate(results)

    resolved = service.resolve_context_reference(actors["planner"], "candidate:" + "a" * 40)

    assert resolved["candidate"]["candidate_id"] == candidate_id


@pytest.mark.anyio
async def test_an_experience_reference_resolves_through_context_resolve(
    service, actors, seeded
) -> None:
    results = await drive_qa_rejection(service, actors, seeded)
    [note_ref] = {
        ref for _role, _tool, result in results for ref in _references(result)
        if ref.startswith("experience:")
    }

    resolved = service.resolve_context_reference(actors["qa"], note_ref)

    assert resolved["note_ref"] == note_ref
    assert resolved["advisory"] is True


def test_the_event_digest_offers_a_ref_only_where_one_resolves(service, actors, seeded) -> None:
    digest = service.list_event_digest(actors["planner"], "project-1")

    types_with_ref = {entry["type"] for entry in digest["changed"] if "ref" in entry}
    types_without = {entry["type"] for entry in digest["changed"] if "ref" not in entry}
    assert "ticket" in types_with_ref
    assert "project" in types_without
    assert types_with_ref <= set(BARE_REFERENCE_KINDS)
    for entry in digest["changed"]:
        if "ref" in entry:
            assert service.resolve_context_reference(actors["planner"], entry["ref"])


@pytest.mark.parametrize(
    "reference, error",
    [
        ("project:project-1", ValidationError),
        ("agent_presence:dev-01", ValidationError),
        ("candidate:", ValidationError),
        ("ctx:finding:", ValidationError),
        ("finding:finding_9999", NotFoundError),
        ("candidate:cand_9999", NotFoundError),
        ("test_run:run_9999", NotFoundError),
        ("experience:exp_9999", NotFoundError),
        ("plain-text", ValidationError),
    ],
)
def test_an_unknown_or_unsupported_reference_is_refused_with_a_reason(
    service, actors, seeded, reference, error
) -> None:
    with pytest.raises(error):
        service.resolve_context_reference(actors["planner"], reference)


@pytest.mark.anyio
async def test_a_reference_outside_the_actor_project_stays_hidden(
    service, actors, seeded
) -> None:
    results = await drive_qa_rejection(service, actors, seeded)
    candidate_id, evidence_refs = _rejected_candidate(results)
    outsider = Actor("dev-elsewhere", Role.DEVELOPER, frozenset({"project-2"}))

    for reference in [f"candidate:{candidate_id}", *evidence_refs]:
        with pytest.raises((AuthorizationError, NotFoundError)):
            service.resolve_context_reference(outsider, reference)
