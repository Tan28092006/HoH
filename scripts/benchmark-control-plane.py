"""Synthetic local timing. Creates a temporary database; never opens a live project."""
import argparse
import json
import statistics
import tempfile
import time
from pathlib import Path

from hoh_control_plane.domain import Actor, Role
from hoh_control_plane.service import ControlPlaneService
from hoh_control_plane.store import SQLiteStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tickets", type=int, default=1000)
    parser.add_argument("--reads", type=int, default=10)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 1 <= args.tickets <= 10000 or not 2 <= args.reads <= 100:
        parser.error("Use 1-10000 tickets and 2-100 reads")
    with tempfile.TemporaryDirectory(prefix="hoh-benchmark-") as temporary:
        store = SQLiteStore(Path(temporary) / "benchmark.db")
        store.initialize()
        service = ControlPlaneService(store)
        planner = Actor("bench-plan", Role.PLANNER, frozenset({"bench"}))
        developer = Actor("bench-dev", Role.DEVELOPER, frozenset({"bench"}))
        service.create_project(planner, "bench", "Synthetic benchmark", "Not production data", "main")
        service.create_feature(planner, "bench", "bench-feature", "Feature", "Timing")
        for index in range(args.tickets):
            service.create_ticket(planner, "bench-feature", f"B-{index}", "Synthetic ticket", "One bounded change",
                                  Role.DEVELOPER, ["Observable result"], [], [f"part-{index}/**"], ready=True)
        service.register_agent(developer, skills=["hoh-developer"])
        results = {"tickets": args.tickets, "reads": args.reads, "store": "sqlite", "concurrency": 1, "operations": {}}
        for name, operation in (
            ("board", lambda: service.board("bench")),
            ("discover_20", lambda: service.discover_work(developer, "bench", limit=20)),
            ("inbox_20", lambda: service.get_agent_inbox(developer, "bench", limit=20)),
        ):
            timings = []
            for _ in range(args.reads):
                started = time.perf_counter()
                output = operation()
                timings.append((time.perf_counter() - started) * 1000)
            results["operations"][name] = {"first_ms": round(timings[0], 2),
                "median_ms": round(statistics.median(timings), 2), "max_ms": round(max(timings), 2),
                "payload_bytes": len(json.dumps(output).encode())}
        with store.connection() as connection:
            results["persisted_offers"] = connection.execute("SELECT COUNT(*) FROM work_offers").fetchone()[0]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(json.dumps(results))


if __name__ == "__main__":
    main()
