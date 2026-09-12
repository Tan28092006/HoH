from __future__ import annotations

import argparse
from pathlib import Path

from hoh_control_plane.service import ControlPlaneService
from hoh_control_plane.store_factory import store_from_environment


def main(argv: list[str] | None = None) -> None:
    """Execute one already-authorized QA test job outside the MCP stdio process."""
    parser = argparse.ArgumentParser(description="Run one Harness Control Plane QA test job")
    parser.add_argument("--database", required=True)
    parser.add_argument("--test-run-id", required=True)
    arguments = parser.parse_args(argv)
    store = store_from_environment(arguments.database)
    ControlPlaneService(store).execute_queued_project_test(arguments.test_run_id)


if __name__ == "__main__":
    main()
