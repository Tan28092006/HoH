"""A database created by the schema before Integrator outcomes became a planning source."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from hoh_control_plane.domain import Actor, Role
from hoh_control_plane.service import ControlPlaneService
from hoh_control_plane.store import SCHEMA, SQLiteStore

# The planner_jobs CHECK as it stands now, and as it stood before an Integrator
# conflict could open a planning job.  Substituting one for the other reproduces
# the legacy file shape from the shipped schema, so this cannot silently drift
# into testing a schema the project no longer has -- legacy_schema() fails loudly
# if the current text is ever reformatted.
_CURRENT_JOB_TYPE_CHECK = (
    "job_type TEXT NOT NULL CHECK (\n"
    "        job_type IN ('qa_failure', 'developer_blocker', 'integration_conflict', 'delivery_completed')\n"
    "    ),"
)
_LEGACY_JOB_TYPE_CHECK = (
    "job_type TEXT NOT NULL CHECK (job_type IN ('qa_failure', 'developer_blocker'))"
    ","
)
_CURRENT_INTERVENTION_TYPE_CHECK = (
    "event_class TEXT NOT NULL CHECK (\n"
    "        event_class IN ('developer_blocker', 'qa_failure', 'integration_conflict', 'delivery_completed')\n"
    "    ),"
)
_LEGACY_INTERVENTION_TYPE_CHECK = (
    "event_class TEXT NOT NULL CHECK (\n"
    "        event_class IN ('developer_blocker', 'qa_failure', 'integration_conflict')\n"
    "    ),"
)


def legacy_schema() -> str:
    if _CURRENT_JOB_TYPE_CHECK not in SCHEMA:
        raise AssertionError(
            "The planner_jobs job_type CHECK no longer matches the text this "
            "regression narrows; update _CURRENT_JOB_TYPE_CHECK so the legacy "
            "upgrade is still actually exercised."
        )
    return SCHEMA.replace(_CURRENT_JOB_TYPE_CHECK, _LEGACY_JOB_TYPE_CHECK).replace(
        _CURRENT_INTERVENTION_TYPE_CHECK, _LEGACY_INTERVENTION_TYPE_CHECK
    )


def write_legacy_database(path: Path) -> None:
    """Create a database exactly as the pre-widening schema would have."""
    connection = sqlite3.connect(path)
    try:
        connection.executescript(legacy_schema())
        connection.commit()
    finally:
        connection.close()


def legacy_service(path: Path) -> ControlPlaneService:
    """A service speaking to a legacy database that has not yet been upgraded."""
    write_legacy_database(path)
    return ControlPlaneService(SQLiteStore(path))


def seed_project(service: ControlPlaneService, planner: Actor) -> dict[str, object]:
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
