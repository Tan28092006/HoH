from __future__ import annotations

import os
from pathlib import Path

import pytest

import hoh_control_plane

_CHECKOUT_SOURCE = (Path(__file__).resolve().parents[1] / "src").resolve()
_IMPORTED_FROM = Path(hoh_control_plane.__file__).resolve().parent

# .venv-hoh has an editable install of an older worktree. Without PYTHONPATH
# naming this checkout, the suite silently tests that tree instead, and a pass
# means nothing. Checked before any other package import, so the message is this
# one and not an ImportError from the stale tree. Use scripts/test.ps1, or set
# HOH_TEST_ALLOW_FOREIGN_PACKAGE=1 when deliberately testing an installed package.
if (
    os.environ.get("HOH_TEST_ALLOW_FOREIGN_PACKAGE") != "1"
    and not _IMPORTED_FROM.is_relative_to(_CHECKOUT_SOURCE)
):
    raise pytest.UsageError(
        f"hoh_control_plane was imported from {_IMPORTED_FROM}, not from this "
        f"checkout ({_CHECKOUT_SOURCE}). Run scripts\\test.ps1 or set "
        f"PYTHONPATH={_CHECKOUT_SOURCE}."
    )


def pytest_addoption(parser):
    parser.addoption("--run-ui", action="store_true", help="Run real browser acceptance tests")
    parser.addoption("--ui-artifacts", default=None, help="Directory outside the candidate for browser evidence")
    parser.addoption("--ui-channel", default=None, help="Installed browser channel, e.g. chrome; default is Playwright Chromium")


# Lanes (review F3). ``git`` marks every test in a module that drives real Git
# repositories; ``slow`` marks the modules whose tests dominate wall time
# (measured with --durations on the dev host). The fast lane is
# ``-m "not slow and not git"``. A test may also carry either marker itself.
_GIT_SIGNS = ('["git"', "['git'", '"git", "-C"', "configure_repository(", "GitWorktreeManager",
              "_tracked_repository(", "_repository(", "repository(tmp_path",
              # tests/helpers setups that build a real repository
              "_candidate_ready_for_integration(", "_candidate_waiting_for_qa(",
              "_gate_ready_candidate(", "queued_merge(", "_rich_ticket(",
              "verified_with_repository(")
SLOW_MODULES = frozenset({
    "test_integration_async", "test_integration_async_gate", "test_integration_gate",
    "test_integration_release_policy", "test_git_worktrees", "test_repository_sync",
    "test_repository_sweep", "test_operational_recovery", "test_supervisor",
    "test_candidate_submission", "test_candidate_requeue", "test_candidate_qa_retry",
    "test_workspace_models", "test_agent_workspace_candidates", "test_qa_runner",
    "test_runner_attestation", "test_runtime_environment", "test_managed_process",
    "test_event_wait", "test_silent_wait", "test_desktop_controls", "test_merge_lane",
    "test_w7_auto_rebase", "test_w7_landing_sweep", "test_w7_mainline_guard",
})
_module_is_git: dict[str, bool] = {}


def _drives_git(path: Path) -> bool:
    key = str(path)
    if key not in _module_is_git:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            text = ""
        _module_is_git[key] = any(sign in text for sign in _GIT_SIGNS)
    return _module_is_git[key]


def pytest_collection_modifyitems(config, items):
    for item in items:
        module = Path(str(item.fspath))
        if module.stem in SLOW_MODULES:
            item.add_marker(pytest.mark.slow)
        if _drives_git(module):
            item.add_marker(pytest.mark.git)
    if not config.getoption("--run-ui"):
        for item in items:
            if "ui" in item.keywords:
                item.add_marker(pytest.mark.skip(reason="Enable browser acceptance with --run-ui"))

from hoh_control_plane.domain import Actor, Role
from hoh_control_plane.service import ControlPlaneService
from hoh_control_plane.store import SQLiteStore


@pytest.fixture(autouse=True)
def delivered_tokens(monkeypatch) -> dict[str, str]:
    """No test writes the operator's real user environment (wave 8A, R4).

    A shared-http profile delivers its token to a Windows user environment
    variable; under test the delivery lands in this dict instead.
    """
    delivered: dict[str, str] = {}
    monkeypatch.setattr("hoh_control_plane.local_credentials.deliver_token",
                        lambda name, token: delivered.__setitem__(name, token))
    monkeypatch.setattr("hoh_control_plane.local_credentials.token_delivery_available", lambda: True)
    import hoh_control_plane.profile_migration as migration
    monkeypatch.setattr(migration, "deliver_token", delivered.__setitem__)
    monkeypatch.setattr(migration, "token_delivery_available", lambda: True)
    return delivered


@pytest.fixture(autouse=True)
def _no_host_environment(monkeypatch):
    """No HOH_* setting of the shell that started pytest reaches a test (review E5).

    A shell with HOH_DATABASE_URL or HOH_DATABASE_PATH set would otherwise point
    every worker's seed data at a real database. HOH_TEST_* settings, which only
    configure the suite itself (e.g. HOH_TEST_POSTGRES_DSN), are kept. A test that
    needs a HOH_* variable sets it through monkeypatch after this runs.
    """
    for key in list(os.environ):
        upper = key.upper()
        if upper.startswith("HOH_") and not upper.startswith("HOH_TEST_"):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture(autouse=True)
def _git_has_no_wall_clock_budget_in_tests(request, monkeypatch):
    """Tests assert what Git did, not how fast this machine ran it.

    Production stops any Git command after 15 seconds. A test repository's
    commands take well under a second, but on a loaded machine (parallel
    workers, a second suite, a paging file under pressure) one can exceed 15
    seconds and the test fails with "Git operation timed out" although nothing
    it asserts is wrong. That made test_repository_sync and test_workspace_models
    fail under load and pass serially. The budget is raised here; a test that is
    about the timeout itself sets its own value, which overrides this one.
    """
    # A test marked production_git_timeout keeps the shipped budget, so at least
    # one test still runs Git under the value production uses (review E5).
    if request.node.get_closest_marker("production_git_timeout") is None:
        monkeypatch.setattr("hoh_control_plane.git_worktrees._GIT_COMMAND_TIMEOUT_SECONDS", 600)


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
