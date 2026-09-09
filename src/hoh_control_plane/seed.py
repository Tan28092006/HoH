from __future__ import annotations

from hoh_control_plane.domain import Actor, ConflictError, Role
from hoh_control_plane.service import ControlPlaneService


def seed_demo(service: ControlPlaneService) -> None:
    """Create a realistic local demo without overwriting existing data."""
    planner = Actor("demo-planner", Role.PLANNER, frozenset({"demo"}))
    try:
        service.create_project(
            planner,
            "demo",
            "Checkout Platform",
            "Multi-agent checkout reliability project",
            "main",
        )
    except ConflictError:
        return
    service.create_feature(
        planner,
        "demo",
        "demo-checkout",
        "Reliable checkout",
        "Make checkout retries observable and idempotent",
        priority=90,
    )
    service.create_ticket(
        planner,
        "demo-checkout",
        "DEV-142",
        "Repair checkout retry behavior",
        "Retry failed payments without duplicate orders",
        Role.DEVELOPER,
        [
            "A failed payment can be retried",
            "A successful retry creates exactly one order",
        ],
        ["Existing successful checkout remains valid"],
        ["src/checkout/**", "tests/checkout/**"],
        True,
        priority=95,
    )
    service.create_ticket(
        planner,
        "demo-checkout",
        "DEV-148",
        "Add idempotency key storage",
        "Persist idempotency keys for payment callbacks",
        Role.DEVELOPER,
        ["Duplicate requests return the original order"],
        ["Retry behavior remains available"],
        ["src/payments/**"],
        True,
        priority=80,
        dependency_ids=["DEV-142"],
    )
    service.create_ticket(
        planner,
        "demo-checkout",
        "DEV-151",
        "Expose retry state in UI",
        "Show retry status to customers",
        Role.DEVELOPER,
        ["Retry state is visible to the customer"],
        ["Keyboard checkout flow remains usable"],
        ["web/checkout/**"],
        True,
        priority=70,
    )
