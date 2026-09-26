from __future__ import annotations

from pathlib import Path

import pytest


def pytest_addoption(parser):
    parser.addoption("--run-ui", action="store_true", help="Run real browser acceptance tests")
    parser.addoption("--ui-artifacts", default=None, help="Directory outside the candidate for browser evidence")
    parser.addoption("--ui-channel", default=None, help="Installed browser channel, e.g. chrome; default is Playwright Chromium")


def pytest_collection_modifyitems(config, items):
    if not config.getoption("--run-ui"):
        for item in items:
            if "ui" in item.keywords:
                item.add_marker(pytest.mark.skip(reason="Enable browser acceptance with --run-ui"))

from hoh_control_plane.domain import Actor, Role
from hoh_control_plane.service import ControlPlaneService
from hoh_control_plane.store import SQLiteStore


@pytest.fixture
def store(tmp_path: Path) -> SQLiteStore:
    database = SQLiteStore(tmp_path / "control-plane.db")
    database.initialize()
    return database


@pytest.fixture
def service(store: SQLiteStore) -> ControlPlaneService:
    return ControlPlaneService(store)


@pytest.fixture
def actors() -> dict[str, Actor]:
    return {
        "planner": Actor("planner-01", Role.PLANNER, frozenset({"project-1"})),
        "developer": Actor("dev-01", Role.DEVELOPER, frozenset({"project-1"})),
        "developer_2": Actor("dev-02", Role.DEVELOPER, frozenset({"project-1"})),
        "qa": Actor("qa-01", Role.QA, frozenset({"project-1"})),
        "integrator": Actor("integrator-01", Role.INTEGRATOR, frozenset({"project-1"})),
    }


@pytest.fixture
def seeded(service: ControlPlaneService, actors: dict[str, Actor]) -> dict[str, object]:
    planner = actors["planner"]
    project = service.create_project(
        planner,
        project_id="project-1",
        name="Checkout Platform",
        description="Reliable checkout",
        default_branch="main",
    )
    feature = service.create_feature(
        planner,
        project_id=project["project_id"],
        feature_id="payment-retry",
        title="Payment retry",
        objective="Let customers retry failed payments safely",
    )
    ticket = service.create_ticket(
        planner,
        feature_id=feature["feature_id"],
        ticket_id="DEV-142",
        title="Repair checkout retry behavior",
        objective="Retry without creating duplicate orders",
        required_role=Role.DEVELOPER,
        acceptance_criteria=[
            "A failed payment can be retried",
            "A successful retry creates exactly one order",
        ],
        preservation_constraints=["Existing successful checkout remains valid"],
        file_scope=["src/checkout/**", "tests/checkout/**"],
        ready=True,
    )
    return {"project": project, "feature": feature, "ticket": ticket}


from hoh_control_plane.capability_probe import ProbeOutcome


class FakeProber:
    """A machine with exactly the equipment a test says it has."""

    def __init__(self, host: str = "qa-host-a",
                 equipped=("browser_interaction", "build_execution")) -> None:
        self.host = host
        self.equipped = set(equipped)
        self.calls: list[str] = []

    def host_names(self) -> set[str]:
        return {self.host}

    def probe(self, capability, *, repository_path, test_command, configured_command):
        self.calls.append(capability)
        if configured_command:
            return ProbeOutcome(True, "exit_zero", "configured_command", 5, "d" * 64)
        if capability in self.equipped:
            return ProbeOutcome(True, "exit_zero", "builtin:test", 5, "c" * 64)
        return ProbeOutcome(False, "no_preflight_defined", "none")
