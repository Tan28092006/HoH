"""Drive HoH's supported test CLI with owned subprocesses and disposable state."""
from __future__ import annotations

import argparse
from contextlib import closing
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tomllib
import uuid
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[4]
TEST = "tests/test_api.py::test_health_board_and_security_headers"
sys.path.insert(0, str(ROOT / "src"))
from hoh_control_plane.managed_process import run_managed
spec = importlib.util.spec_from_file_location("ci_evidence", ROOT / "scripts/verify-ci-evidence.py")
ci_evidence = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci_evidence)


def safe_root(path: Path) -> Path:
    path = path.resolve()
    forbidden = [ROOT, Path("D:/HoH/data"), Path("D:/HoH/tmp"), Path("D:/HoH-service")]
    if any(path.is_relative_to(parent.resolve()) for parent in forbidden):
        raise ValueError("Verification scratch/evidence must be outside candidate and protected paths")
    return path


def environment() -> dict[str, str]:
    result = os.environ.copy()
    for key in list(result):
        if key.upper().startswith(("HOH_", "COV_CORE", "COVERAGE")) or key.upper() in {
            "PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTHONHOME", "PYTHONSTARTUP"
        }:
            result.pop(key)
    result.update(PYTHONPATH=str(ROOT / "src"), PYTHONUTF8="1")
    return result


def doctor(python: str, output: Path, env: dict[str, str]) -> dict:
    command = [python, "-c", "import json,os,sys,pytest,hoh_control_plane as h;"
               "print(json.dumps(dict(pid=os.getpid(),python=sys.executable,"
               "version=h.__version__,source=h.__file__,pytest=pytest.__version__)))"]
    result = run_managed(command, cwd=ROOT, timeout=30, env=env)
    (output / "doctor.log").write_text(result.stdout + result.stderr, encoding="utf-8")
    if result.returncode != 0 or result.timed_out or result.cancelled or result.capture_errors or result.stdout_truncated or result.stderr_truncated:
        raise ValueError("Pinned CLI interpreter is not ready")
    observed = json.loads(result.stdout)
    if Path(observed["source"]).resolve() != ROOT / "src/hoh_control_plane/__init__.py":
        raise ValueError("Doctor observed a foreign source tree")
    if Path(observed["python"]).resolve() != Path(python).resolve():
        raise ValueError("Doctor observed a different interpreter")
    if observed["version"] != tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]:
        raise ValueError("Doctor observed a different package version")
    observed["revision"] = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    observed["command"] = command
    observed["source_snapshot"] = ci_evidence.source_snapshot()
    observed["surface"] = "short-lived CLI; no listening port"
    (output / "doctor.json").write_text(json.dumps(observed, indent=2), encoding="utf-8")
    return observed


def verify(python: str, evidence_root: Path, scratch_root: Path, only_doctor: bool) -> Path:
    run_id = uuid.uuid4().hex[:12]
    proof = safe_root(evidence_root) / run_id
    scratch = safe_root(scratch_root) / run_id
    proof.mkdir(parents=True, exist_ok=False)
    scratch.mkdir(parents=True, exist_ok=False)
    env = environment()
    # Logs are retained outside scratch even when the wrapper cleans its own run.
    env.update(HOH_TEST_PYTHON=python, HOH_TEST_TEMP_ROOT=str(proof / "runner"))
    try:
        identity = doctor(python, proof, env)
        if not only_doctor:
            powershell = shutil.which("powershell") or shutil.which("pwsh")
            if not powershell:
                raise ValueError("The supported CLI requires PowerShell")
            report = proof / "tests.xml"
            command = [powershell, "-NoProfile", "-NonInteractive", "-File",
                       str(ROOT / "scripts/test.ps1"), TEST, "-n", "0",
                       f"--basetemp={scratch}", f"--junitxml={report}",
                       "-o", f"junit_suite_name=hoh-{identity['revision']}-targeted"]
            result = run_managed(command, cwd=ROOT, timeout=300, env=env,
                                 output_limit=2_000_000)
            (proof / "drive.log").write_text(result.stdout + result.stderr, encoding="utf-8")
            (proof / "drive.json").write_text(json.dumps({"command": command,
                "exit_code": result.returncode, "timed_out": result.timed_out,
                "cancelled": result.cancelled, "stdout_truncated": result.stdout_truncated,
                "stderr_truncated": result.stderr_truncated,
                "capture_errors": result.capture_errors,
                "source_snapshot_after": ci_evidence.source_snapshot()}, indent=2), encoding="utf-8")
            if ci_evidence.source_snapshot() != identity["source_snapshot"] or ci_evidence.revision() != identity["revision"]:
                raise ValueError("Candidate revision or source changed during the drive")
            if result.returncode != 0 or result.timed_out or result.cancelled or result.capture_errors or result.stdout_truncated or result.stderr_truncated:
                raise ValueError("Actual targeted CLI drive did not pass; see retained log")
            cases = list(ET.parse(report).getroot().iter("testcase"))
            if len(cases) != 1 or cases[0].get("name") != "test_health_board_and_security_headers" or len(cases[0]):
                raise ValueError("Targeted drive did not execute the one required passing case")
            databases = list(scratch.rglob("api.db"))
            if len(databases) != 1:
                raise ValueError("Targeted drive did not leave exactly one isolated fixture database")
            database = databases[0]
            with closing(sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)) as connection:
                projects = connection.execute("SELECT project_id, name FROM projects WHERE project_id='demo'").fetchall()
                events = connection.execute("SELECT event_type FROM audit_events WHERE event_type='project.created' AND aggregate_id='demo'").fetchall()
                if len(projects) != 1 or len(events) != 1:
                    raise ValueError("Expected actual fixture side effects are absent")
                with closing(sqlite3.connect(proof / "fixture-proof.db")) as backup:
                    connection.backup(backup)
            (proof / "feature-proof.json").write_text(json.dumps({"feature": "targeted-lane",
                "test": TEST, "projects": projects, "events": events,
                "database": str(database), "server_port": None}, indent=2), encoding="utf-8")
    finally:
        # run_managed closes only its owned Job Object/process group, including
        # on failures. Never kill by name or operate on a persisted PID.
        cleanup = {"scratch_removed": False,
            "process_ownership": "run_managed: Windows Job Object / POSIX process group"}
        try:
            owned_root = safe_root(scratch_root)
            target = scratch.resolve(strict=True)
            if target != scratch or target.parent != owned_root or scratch.name != run_id or scratch.is_symlink() or scratch.is_junction() or target.is_relative_to(proof.resolve()):
                raise ValueError("Scratch resolved path or ownership changed; refusing cleanup")
            safe_root(target)
            shutil.rmtree(target)
            cleanup["scratch_removed"] = not scratch.exists()
        except (ValueError, OSError) as error:
            cleanup["error"] = str(error)
            raise
        finally:
            cleanup["evidence_retained"] = sorted(path.name for path in proof.iterdir())
            (proof / "cleanup.json").write_text(json.dumps(cleanup, indent=2), encoding="utf-8")
    return proof


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("doctor", "verify"))
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--scratch-root", type=Path, required=True)
    args = parser.parse_args()
    try:
        proof = verify(args.python, args.evidence_root, args.scratch_root, args.action == "doctor")
        print(json.dumps({"status": "passed", "evidence": str(proof)}))
        return 0
    except (ValueError, OSError, ET.ParseError) as error:
        print(f"Verification failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
