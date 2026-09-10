from __future__ import annotations

import argparse
from pathlib import Path

from hoh_control_plane.service import ControlPlaneService
from hoh_control_plane.store import SQLiteStore


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Validate and freeze one queued candidate")
    parser.add_argument("--database", required=True)
    parser.add_argument("--submission-id", required=True)
    arguments = parser.parse_args(argv)
    store = SQLiteStore(Path(arguments.database))
    store.initialize()
    ControlPlaneService(store).execute_queued_candidate_submission(arguments.submission_id)


if __name__ == "__main__":
    main()
