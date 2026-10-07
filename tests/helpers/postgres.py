"""A service on a throwaway PostgreSQL schema; skipped without HOH_TEST_POSTGRES_DSN."""
from __future__ import annotations

import os
import uuid

import pytest

from hoh_control_plane.domain import Actor, Role
from hoh_control_plane.service import ControlPlaneService

psycopg = pytest.importorskip("psycopg", reason="PostgreSQL driver is not installed")

DSN = os.getenv("HOH_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(
    not DSN, reason="set HOH_TEST_POSTGRES_DSN to run the PostgreSQL conformance suite"
)


@pytest.fixture
def pg_service():
    """A service on its own PostgreSQL schema, dropped afterwards."""
    from hoh_control_plane.postgres_store import PostgresStore, translate_schema

    schema = f"hoh_test_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(DSN, autocommit=True) as admin:
        admin.execute(f'CREATE SCHEMA "{schema}"')
    store = PostgresStore(DSN)
    original_connect = store._connect

    def scoped():
        connection = original_connect()
        connection.execute(f'SET search_path TO "{schema}"')
        return connection

    store._connect = scoped
    store.initialize()
    try:
        yield ControlPlaneService(store)
    finally:
        with psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute(f'DROP SCHEMA "{schema}" CASCADE')


@pytest.fixture
def pg_seeded(pg_service):
    planner = Actor("planner-pg", Role.PLANNER, frozenset({"project-pg"}))
    pg_service.create_project(
        planner, project_id="project-pg", name="PG", description="", default_branch="main"
    )
    pg_service.create_feature(
        planner, project_id="project-pg", feature_id="f-pg", title="F", objective="O"
    )
    ticket = pg_service.create_ticket(
        planner,
        feature_id="f-pg",
        ticket_id="PG-1",
        title="Work on PostgreSQL",
        objective="Prove the guarantees survive the backend change",
        required_role=Role.DEVELOPER,
        acceptance_criteria=["It behaves as it did on SQLite"],
        preservation_constraints=[],
        file_scope=["src/**"],
        ready=True,
    )
    return {"planner": planner, "ticket": ticket}


def _developer(suffix: str = "1") -> Actor:
    return Actor(f"dev-pg-{suffix}", Role.DEVELOPER, frozenset({"project-pg"}))
