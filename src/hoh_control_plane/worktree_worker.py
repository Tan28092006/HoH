from __future__ import annotations

import argparse
from pathlib import Path

from hoh_control_plane.service import ControlPlaneService
from hoh_control_plane.store_factory import store_from_environment


def main(argv: list[str] | None = None) -> None:
    """Complete one queued Git worktree provision outside the MCP stdio server."""
    parser = argparse.ArgumentParser(description="Provision one Harness Control Plane worktree")
    parser.add_argument("--database", required=True)
    parser.add_argument("--provision-id", required=True)
    arguments = parser.parse_args(argv)
    store = store_from_environment(arguments.database)
    ControlPlaneService(store).execute_queued_worktree_provision(arguments.provision_id)


if __name__ == "__main__":
    main()
