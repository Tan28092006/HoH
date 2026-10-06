"""Every MCP parameter the service compares against a fixed set carries that set (wave 8D, A3).

A str parameter whose valid values live only in a service check costs an agent a
validation_error round trip per wrong guess. Each such parameter is now a
Literal built from the same constant the service checks, so the JSON schema an
agent reads offers exactly the values the service accepts.
"""
from __future__ import annotations

import re
from typing import Any

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from hoh_control_plane import contracts, event_wait
from hoh_control_plane.domain import Actor, Role, TicketStatus, ValidationError, WorkspaceModel
from hoh_control_plane.mcp_server import create_mcp_server
from hoh_control_plane.service._common import CONTEXT_ENTRY_TYPES
from hoh_control_plane.service.discovery import DiscoveryMixin
from hoh_control_plane.service.planner_jobs import _FAILED_JOB_TABLES

_EXECUTION_ROLES = {role.value for role in DiscoveryMixin._ROLE_ACTIONABLE_STATUSES}

# (tool, parameter) -> the value set the service itself checks.
ENUM_PARAMETERS: dict[tuple[str, str], set[str]] = {
    ("work.report_failure", "failure_class"): set(contracts.FAILURE_CLASSES),
    ("work.report_failure", "job_kind"): set(_FAILED_JOB_TABLES),
    ("work.wait_for_event", "job_kind"): set(event_wait.JOBS),
    ("work.discover", "awaiting_role"): _EXECUTION_ROLES,
    ("work.discover", "status"): {status.value for status in TicketStatus},
    ("work.report_usage", "worker"): set(contracts.WORKER_KINDS),
    ("planner.create_ticket", "required_role"): _EXECUTION_ROLES,
    ("planner.create_ticket", "ticket_type"): set(contracts.TICKET_TYPES),
    ("planner.resume_ticket_after_failure", "resume_role"): _EXECUTION_ROLES,
    ("planner.retire_ticket_for_reconciliation", "target_status"): {
        TicketStatus.CANCELLED.value, TicketStatus.SUPERSEDED.value
    },
    ("planner.append_context", "entry_type"): set(contracts.PLANNER_ENTRY_TYPES),
    ("planner.list_repository_reconciliations", "status"): set(
        contracts.RECONCILIATION_STATUSES
    ),
    ("planner.configure_repository", "workspace_model"): {model.value for model in WorkspaceModel},
    ("planner.configure_verification_profile", "quality_policy"): set(
        contracts.QUALITY_POLICIES
    ),
    ("qa.submit_verdict", "verdict"): set(contracts.VERDICTS),
}

# A parameter whose name says "one of a few values" must carry an enum.
_ENUM_SHAPED_NAME = re.compile(
    r"(^|_)(class|kind|role|type|status|policy|model|verdict|worker|disposition)$"
)
# Parameters that are open by design, and why; kept honest by a test below.
OPEN_BY_DESIGN = {
    ("agent.register", "model_family"): "a portable family name, normalized, not a closed set",
    ("agent.register", "model"): "the client's model name, recorded as reported",
    ("planner.resolve_job", "resolution"): "Planner's free-text record of the decision",
    ("planner.resume_ticket_after_failure", "resolution"): "free-text record of the decision",
    ("planner.requeue_candidate", "reason"): "free-text reason, bounded only by length",
}


def _role_actors() -> list[Actor]:
    return [Actor(f"enum-{role.value}", role, frozenset({"project-1"})) for role in Role]


async def _schemas(service) -> dict[str, dict[str, Any]]:
    schemas: dict[str, dict[str, Any]] = {}
    for actor in _role_actors():
        for tool in await create_mcp_server(service, actor).list_tools():
            schemas[tool.name] = tool.inputSchema
    return schemas


def _enum_values(schema: dict[str, Any], node: Any) -> set[str] | None:
    """The enum a property offers, through Optional, arrays and $ref."""
    if isinstance(node, dict):
        if "enum" in node:
            return set(node["enum"])
        if "const" in node:
            return {node["const"]}
        if "$ref" in node:
            return _enum_values(schema, schema["$defs"][node["$ref"].rsplit("/", 1)[-1]])
        for key in ("anyOf", "oneOf", "allOf"):
            for option in node.get(key, []):
                found = _enum_values(schema, option)
                if found is not None:
                    return found
        if "items" in node:
            return _enum_values(schema, node["items"])
    return None


@pytest.mark.anyio
@pytest.mark.parametrize("tool, parameter", sorted(ENUM_PARAMETERS))
async def test_each_enumerated_parameter_offers_exactly_the_service_value_set(
    service, tool, parameter
) -> None:
    schema = (await _schemas(service))[tool]

    offered = _enum_values(schema, schema["properties"][parameter])

    assert offered == ENUM_PARAMETERS[(tool, parameter)]


@pytest.mark.anyio
async def test_every_enum_shaped_parameter_on_every_role_carries_an_enum(service) -> None:
    missing = []
    for tool, schema in sorted((await _schemas(service)).items()):
        for parameter, node in schema.get("properties", {}).items():
            if (tool, parameter) in OPEN_BY_DESIGN or not _ENUM_SHAPED_NAME.search(parameter):
                continue
            if _enum_values(schema, node) is None:
                missing.append(f"{tool}.{parameter}")

    assert missing == []


@pytest.mark.anyio
async def test_open_parameters_are_named_only_while_they_exist_and_stay_open(service) -> None:
    schemas = await _schemas(service)
    for tool, parameter in OPEN_BY_DESIGN:
        schema = schemas[tool]
        assert _enum_values(schema, schema["properties"][parameter]) is None, (tool, parameter)


def test_the_literal_sets_match_the_domain_enumerations() -> None:
    assert contracts.TICKET_STATUSES == {status.value for status in TicketStatus}
    assert contracts.WORKSPACE_MODELS == {model.value for model in WorkspaceModel}
    assert contracts.EXECUTION_ROLES == _EXECUTION_ROLES
    assert contracts.WAIT_JOB_KINDS == set(event_wait.JOBS)
    assert contracts.FAILED_JOB_KINDS == set(_FAILED_JOB_TABLES)
    assert contracts.RETIREMENT_STATUSES <= contracts.TICKET_STATUSES
    # Planner writes a subset of the closed context vocabulary, never a new type.
    assert contracts.PLANNER_ENTRY_TYPES <= CONTEXT_ENTRY_TYPES


@pytest.mark.anyio
@pytest.mark.parametrize(
    "tool, arguments",
    [
        ("work.report_failure", {
            "project_id": "project-1", "ticket_id": "DEV-1", "failure_class": "flaky",
            "summary": "It broke",
        }),
        ("work.report_failure", {
            "project_id": "project-1", "ticket_id": "DEV-1", "failure_class": "infrastructure",
            "summary": "It broke", "job_kind": "qa", "job_id": "run_1",
        }),
        ("work.discover", {"project_id": "project-1", "awaiting_role": "planner"}),
        ("work.discover", {"project_id": "project-1", "status": ["ready"]}),
    ],
)
async def test_an_out_of_set_value_is_refused_before_the_service_runs(
    service, actors, seeded, tool, arguments
) -> None:
    server = create_mcp_server(service, actors["developer"])
    with pytest.raises(ToolError):
        await server.call_tool(tool, arguments)
    with service.store.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM planner_jobs").fetchone()[0] == 0


@pytest.mark.anyio
async def test_planner_entry_type_is_closed_in_the_schema_but_the_service_keeps_aliases(
    service, actors, seeded
) -> None:
    server = create_mcp_server(service, actors["planner"])
    ticket_id = seeded["ticket"]["ticket_id"]
    with pytest.raises(ToolError):
        await server.call_tool("planner.append_context", {
            "ticket_id": ticket_id, "entry_type": "planning_decision", "content": "Split it.",
        })

    # A REST or in-process caller still lands an alias on its vocabulary type.
    entry = service.append_planner_context(
        actors["planner"], entry_type="planning_decision", content="Split it.",
        ticket_id=ticket_id,
    )
    assert entry["entry_type"] == "planner_decision"


def test_the_service_refuses_a_ticket_type_outside_the_vocabulary(service, actors, seeded) -> None:
    with pytest.raises(ValidationError, match="ticket_type"):
        service.create_ticket(
            actors["planner"], feature_id=seeded["feature"]["feature_id"], ticket_id="DEV-9",
            title="Chore", objective="Tidy up", required_role=Role.DEVELOPER,
            acceptance_criteria=["It is tidy"], preservation_constraints=[],
            file_scope=["src/**"], ticket_type="chore",
        )


@pytest.mark.anyio
async def test_skill_manifest_is_a_typed_list_of_four_named_fields(service, actors) -> None:
    schemas = await _schemas(service)
    schema = schemas["agent.register"]
    node = schema["properties"]["skill_manifest"]
    array = next(option for option in node["anyOf"] if option.get("type") == "array")
    item = array["items"]
    if "$ref" in item:
        item = schema["$defs"][item["$ref"].rsplit("/", 1)[-1]]

    assert set(item["properties"]) == {"name", "version", "checksum", "loaded_at"}
    assert set(item["required"]) == {"name", "version", "checksum", "loaded_at"}
    assert item.get("additionalProperties") is False
    assert array["maxItems"] == contracts.MAX_SKILL_MANIFEST_ENTRIES


@pytest.mark.anyio
async def test_a_skill_manifest_entry_with_a_wrong_key_is_refused(service, actors) -> None:
    server = create_mcp_server(service, actors["developer"])
    entry = {"name": "hoh-developer", "version": "1.0.0", "checksum": "a" * 64,
             "loaded_at": "2026-10-06T09:00:00Z"}

    _content, registered = await server.call_tool(
        "agent.register", {"skills": ["hoh-developer"], "skill_manifest": [entry]}
    )
    [recorded] = registered["skill_manifest"]
    assert (recorded["name"], recorded["version"]) == ("hoh-developer", "1.0.0")
    with pytest.raises(ToolError):
        await server.call_tool(
            "agent.register",
            {"skills": ["hoh-developer"], "skill_manifest": [{**entry, "role": "planner"}]},
        )


@pytest.mark.anyio
async def test_docstrings_name_the_roles_and_keys_the_code_uses(service) -> None:
    tools = {}
    for actor in _role_actors():
        for tool in await create_mcp_server(service, actor).list_tools():
            tools[tool.name] = tool

    assert "Integrator" in tools["work.heartbeat"].description
    assert "ticket_id" in tools["experience.record"].inputSchema["properties"]
    assert "ticket_id" in tools["experience.record"].description
    assert "Example" in tools["work.report_failure"].description
