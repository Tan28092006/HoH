"""Skill and protocol compatibility enforcement at agent registration (HOH-216)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hoh_control_plane.domain import (
    Actor,
    AuthorizationError,
    Role,
    SkillCompatibilityError,
    ValidationError,
)
from hoh_control_plane.service import ControlPlaneService
from hoh_control_plane.store import SCHEMA, SQLiteStore


def _manifest(name: str = "hoh-developer", version: str = "1.0.0") -> list[dict[str, str]]:
    return [
        {
            "name": name,
            "version": version,
            "checksum": "a" * 64,
            "loaded_at": "2026-09-11T00:00:00Z",
        }
    ]


def test_registration_is_compatible_by_default_without_a_configured_policy(
    service, actors, seeded
) -> None:
    presence = service.register_agent(
        actors["developer"],
        skills=["hoh-developer"],
        skill_manifest=_manifest(version="9.9.9"),
        protocol_version="0.1.0",
    )

    assert presence["skill_compatibility"]["project-1"]["compatible"] is True
    claim = service.claim_ticket(
        actors["developer"], seeded["ticket"]["ticket_id"], seeded["ticket"]["version"], 900
    )
    assert claim["ticket"]["status"] == "DEV_IN_PROGRESS"


def test_planner_configures_and_reads_back_a_skill_policy(service, actors, seeded) -> None:
    configured = service.configure_skill_policy(
        actors["planner"],
        "project-1",
        accepted_protocol_versions=["0.3.0"],
        accepted_skill_versions={"hoh-developer": ["2.0.0", "2.1.0"]},
    )

    assert configured["policy"]["accepted_protocol_versions"] == ["0.3.0"]
    assert configured["policy"]["accepted_skill_versions"] == {
        "hoh-developer": ["2.0.0", "2.1.0"]
    }

    read_back = service.get_skill_compatibility(actors["developer"], "project-1")
    assert read_back["policy"]["accepted_protocol_versions"] == ["0.3.0"]


def test_a_stale_skill_version_is_incompatible_and_blocks_claiming(
    service, actors, seeded
) -> None:
    service.configure_skill_policy(
        actors["planner"],
        "project-1",
        accepted_protocol_versions=["0.3.0"],
        accepted_skill_versions={"hoh-developer": ["2.0.0"]},
    )

    service.register_agent(
        actors["developer"],
        skills=["hoh-developer"],
        skill_manifest=_manifest(version="1.0.0"),
        protocol_version="0.3.0",
    )

    with pytest.raises(SkillCompatibilityError) as excinfo:
        service.claim_ticket(
            actors["developer"], seeded["ticket"]["ticket_id"], seeded["ticket"]["version"], 900
        )
    assert excinfo.value.code == "skill_incompatible"


def test_no_declared_manifest_is_incompatible_once_a_policy_requires_one(
    service, actors, seeded
) -> None:
    service.configure_skill_policy(
        actors["planner"],
        "project-1",
        accepted_protocol_versions=[],
        accepted_skill_versions={"hoh-developer": ["2.0.0"]},
    )

    presence = service.register_agent(actors["developer"], skills=["hoh-developer"])

    assert presence["skill_compatibility"]["project-1"]["compatible"] is False
    with pytest.raises(SkillCompatibilityError):
        service.claim_ticket(
            actors["developer"], seeded["ticket"]["ticket_id"], seeded["ticket"]["version"], 900
        )


def test_a_compatible_upgrade_clears_incompatibility_and_unblocks_claiming(
    service, actors, seeded
) -> None:
    service.configure_skill_policy(
        actors["planner"],
        "project-1",
        accepted_protocol_versions=[],
        accepted_skill_versions={"hoh-developer": ["2.0.0"]},
    )
    service.register_agent(
        actors["developer"], skills=["hoh-developer"], skill_manifest=_manifest(version="1.0.0")
    )
    with pytest.raises(SkillCompatibilityError):
        service.claim_ticket(
            actors["developer"], seeded["ticket"]["ticket_id"], seeded["ticket"]["version"], 900
        )

    upgraded = service.register_agent(
        actors["developer"], skills=["hoh-developer"], skill_manifest=_manifest(version="2.0.0")
    )
    assert upgraded["skill_compatibility"]["project-1"]["compatible"] is True

    claim = service.claim_ticket(
        actors["developer"], seeded["ticket"]["ticket_id"], seeded["ticket"]["version"], 900
    )
    assert claim["ticket"]["status"] == "DEV_IN_PROGRESS"


def test_a_stale_protocol_version_blocks_claiming_even_with_accepted_skills(
    service, actors, seeded
) -> None:
    service.configure_skill_policy(
        actors["planner"],
        "project-1",
        accepted_protocol_versions=["0.3.0"],
        accepted_skill_versions={},
    )
    service.register_agent(
        actors["developer"],
        skills=["hoh-developer"],
        skill_manifest=_manifest(version="2.0.0"),
        protocol_version="0.1.0",
    )

    with pytest.raises(SkillCompatibilityError):
        service.claim_ticket(
            actors["developer"], seeded["ticket"]["ticket_id"], seeded["ticket"]["version"], 900
        )


def test_an_incompatible_agent_still_gets_safe_discovery(service, actors, seeded) -> None:
    service.configure_skill_policy(
        actors["planner"],
        "project-1",
        accepted_protocol_versions=[],
        accepted_skill_versions={"hoh-developer": ["2.0.0"]},
    )

    presence = service.register_agent(
        actors["developer"], skills=["hoh-developer"], skill_manifest=_manifest(version="1.0.0")
    )
    assert presence["skill_compatibility"]["project-1"]["compatible"] is False

    inbox = service.get_agent_inbox(actors["developer"], "project-1")
    assert inbox["agent"]["agent_id"] == actors["developer"].agent_id
    discovered = service.discover_work(actors["developer"], "project-1")
    assert discovered["project"]["project_id"] == "project-1"


def test_a_declared_manifest_never_grants_role_permission(service, actors, seeded) -> None:
    """Forged role metadata: nothing in skill_manifest can widen an actor's role."""
    presence = service.register_agent(
        actors["developer"],
        skills=["hoh-developer"],
        skill_manifest=[
            {
                "name": "hoh-qa",
                "version": "1.0.0",
                "checksum": "b" * 64,
                "loaded_at": "2026-09-11T00:00:00Z",
            },
            {
                "name": "hoh-planner",
                "version": "1.0.0",
                "checksum": "c" * 64,
                "loaded_at": "2026-09-11T00:00:00Z",
            },
        ],
    )

    assert presence["role"] == "developer"
    with pytest.raises(AuthorizationError):
        service.cancel_ticket(
            actors["developer"], seeded["ticket"]["ticket_id"], seeded["ticket"]["version"], "No."
        )
    with pytest.raises(AuthorizationError):
        service.configure_skill_policy(
            actors["developer"], "project-1", accepted_protocol_versions=[], accepted_skill_versions={}
        )
    assert "ticket:plan" not in service.capabilities(actors["developer"])


def test_configure_skill_policy_refuses_cross_project_access_and_wrong_role(
    service, actors, seeded
) -> None:
    cross_project_admin = Actor("admin-setup", Role.ADMIN, frozenset({"*"}))
    service.create_project(cross_project_admin, "project-2", "Other", "Other project", "main")

    with pytest.raises(AuthorizationError):
        service.configure_skill_policy(
            actors["planner"],
            "project-2",
            accepted_protocol_versions=["0.3.0"],
            accepted_skill_versions={},
        )
    with pytest.raises(AuthorizationError):
        service.configure_skill_policy(
            actors["developer"],
            "project-1",
            accepted_protocol_versions=["0.3.0"],
            accepted_skill_versions={},
        )


def test_get_skill_compatibility_refuses_without_project_access(service, actors, seeded) -> None:
    outsider = Actor("outsider-01", Role.DEVELOPER, frozenset({"project-9"}))

    with pytest.raises(AuthorizationError):
        service.get_skill_compatibility(outsider, "project-1")


def test_skill_manifest_validation_rejects_malformed_entries(service, actors, seeded) -> None:
    with pytest.raises(ValidationError):
        service.register_agent(
            actors["developer"],
            skills=["hoh-developer"],
            skill_manifest=[{"name": "hoh-developer", "version": "1.0.0"}],
        )
    with pytest.raises(ValidationError):
        service.register_agent(
            actors["developer"],
            skills=["hoh-developer"],
            skill_manifest=[
                {
                    "name": "hoh-developer",
                    "version": "1.0.0",
                    "checksum": "a" * 64,
                    "loaded_at": "not-a-timestamp",
                }
            ],
        )
    with pytest.raises(ValidationError):
        service.register_agent(
            actors["developer"],
            skills=["hoh-developer"],
            skill_manifest=_manifest() + _manifest(),
        )


def test_configure_skill_policy_bounds_its_inputs(service, actors, seeded) -> None:
    with pytest.raises(ValidationError):
        service.configure_skill_policy(
            actors["planner"],
            "project-1",
            accepted_protocol_versions=["0.3.0"],
            accepted_skill_versions={"hoh-developer": []},
        )


_CURRENT_AGENT_PRESENCE_COLUMNS = (
    "    updated_at TEXT NOT NULL,\n"
    "    skill_manifest TEXT NOT NULL DEFAULT '[]',\n"
    "    protocol_version TEXT,\n"
    "    skill_compatibility TEXT NOT NULL DEFAULT '{}',\n"
    "    -- Where to knock. An agent that registers none is not broken: it polls,\n"
    "    -- which stays a complete path on its own.\n"
    "    endpoint TEXT,\n"
    "    -- When this agent last registered, i.e. when its current chat began.\n"
    "    session_started_at TEXT\n"
    ");"
)
_LEGACY_AGENT_PRESENCE_COLUMNS = "    updated_at TEXT NOT NULL\n);"


def _legacy_schema() -> str:
    if _CURRENT_AGENT_PRESENCE_COLUMNS not in SCHEMA:
        raise AssertionError(
            "agent_presence's column list no longer matches the text this regression "
            "narrows; update _CURRENT_AGENT_PRESENCE_COLUMNS so the legacy upgrade path "
            "this test protects is still actually exercised."
        )
    return SCHEMA.replace(_CURRENT_AGENT_PRESENCE_COLUMNS, _LEGACY_AGENT_PRESENCE_COLUMNS)


def test_a_legacy_database_without_skill_columns_upgrades_cleanly(tmp_path: Path) -> None:
    """A database created before HOH-216 lacks these columns; ADD COLUMN must heal it."""
    path = tmp_path / "legacy-control-plane.db"
    connection = sqlite3.connect(path)
    try:
        connection.executescript(_legacy_schema())
        connection.commit()
    finally:
        connection.close()

    before = sqlite3.connect(path)
    try:
        columns_before = {row[1] for row in before.execute("PRAGMA table_info(agent_presence)")}
    finally:
        before.close()
    assert "skill_manifest" not in columns_before

    SQLiteStore(path).initialize()

    after = sqlite3.connect(path)
    try:
        columns_after = {row[1] for row in after.execute("PRAGMA table_info(agent_presence)")}
    finally:
        after.close()
    assert {
        "skill_manifest", "protocol_version", "skill_compatibility", "session_started_at",
    } <= columns_after

    service = ControlPlaneService(SQLiteStore(path))
    planner = Actor("planner-01", Role.PLANNER, frozenset({"project-1"}))
    developer = Actor("dev-01", Role.DEVELOPER, frozenset({"project-1"}))
    service.create_project(planner, "project-1", "Project", "Description", "main")
    presence = service.register_agent(
        developer, skills=["hoh-developer"], skill_manifest=_manifest()
    )
    assert presence["skill_manifest"][0]["name"] == "hoh-developer"
