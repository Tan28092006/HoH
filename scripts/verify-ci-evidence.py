"""Run the existing CI lane and reject incomplete or substituted evidence."""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
FAST_MARKERS = "not slow and not git"
POSTGRES_CASES = {
    "test_the_schema_lands_whole_on_postgres", "test_statements_written_in_qmark_style_run_unchanged",
    "test_a_claim_is_exclusive", "test_a_queue_offer_is_handed_out_once",
    "test_a_stale_expected_version_is_rejected", "test_an_idempotency_record_replays_rather_than_duplicating",
    "test_a_constraint_violation_arrives_as_the_control_plane_s_own_error",
    "test_the_monotonic_sequence_column_still_orders_events", "test_foreign_keys_are_enforced_without_being_switched_on",
    "test_sqlite_remains_the_default_and_needs_no_driver", "test_experience_notes_survive_the_backend",
    "test_a_lesson_reaches_an_overlapping_ticket_on_postgres", "test_the_spec_index_runs_on_postgres",
    "test_the_outline_depth_filter_runs_on_postgres", "test_an_older_postgres_database_gains_the_columns_it_is_missing",
    "test_the_upgraded_postgres_database_still_works", "test_migrating_a_postgres_database_twice_changes_nothing",
    "test_postgres_records_and_enforces_the_schema_version", "test_the_experience_index_is_rebuilt_on_postgres_too",
    "test_evidence_records_how_its_checksum_was_obtained_on_postgres",
}
UI_CASES = {
    *(f"test_operator_can_follow_ticket_and_filter_state[viewport{i}]" for i in range(3)),
    "test_background_refresh_preserves_ticket_keyboard_focus",
    "test_old_response_cannot_replace_a_newer_snapshot", "test_empty_project_and_error_are_observable",
}
DEV_SCRIPT_CASES = {
    *(f"test_the_real_database_is_refused[{database}-{script}]"
      for database in (r"D:\HoH\data\control-plane.db", r"D:\HOH\Data\control-plane.db", r"D:\HoH-service\data\control-plane.db")
      for script in ("start-dashboard.ps1", "start-scheduler.ps1", "start-supervisor.ps1")),
    "test_this_checkouts_data_directory_is_refused_through_the_environment",
    "test_the_installed_hosts_ports_are_refused[8765]", "test_the_installed_hosts_ports_are_refused[8931]",
    "test_a_dev_path_resolves_and_its_folder_is_created",
}


def revision() -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
    ).strip()


def source_snapshot() -> dict:
    """Attribute dirty development runs as well as pristine CI revisions."""
    paths = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).decode().split("\0")
    # These may be untracked/ignored during implementation; include only owned
    # executable and test files, never arbitrary local client configuration.
    paths += ["scripts/verify-ci-evidence.py", "tests/test_ci_evidence.py", "tests/test_test_runner.py"]
    paths += [str(path.relative_to(ROOT)).replace("\\", "/")
              for path in (ROOT / ".codex/skills/verify-hoh-ci").rglob("*")
              if path.is_file() and "__pycache__" not in path.parts]
    digest = hashlib.sha256()
    for name in sorted(set(paths) - {""}):
        path = ROOT / name
        digest.update(name.encode() + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest() if path.is_file() else b"missing")
    dirty = subprocess.check_output(["git", "status", "--porcelain", "-z"], cwd=ROOT).decode()
    return {"files_sha256": digest.hexdigest(), "dirty_status": dirty}


def allowed_skip(case: ET.Element, platform: str, checkout: str) -> bool:
    skipped = case.find("skipped")
    if skipped is None:
        return False
    module, name = case.get("classname", ""), case.get("name", "")
    reason = skipped.get("message", "")
    if not module and name in {"tests.test_postgres_store", "tests.test_postgres_concurrency"} and reason == "collection skipped" and skipped.get("type") is None:
        try:
            location, line, detail = ast.literal_eval(skipped.text or "")
            return Path(location).resolve() == ROOT / "tests/helpers/postgres.py" and line == 12 and detail == "Skipped: PostgreSQL driver is not installed"
        except (ValueError, SyntaxError, TypeError):
            return False
    if skipped.get("type") != "pytest.skip":
        return False
    # Collection-time importorskip uses the module name as the case name.
    collection_skip = not module
    if collection_skip:
        module = name.removesuffix(".py").replace("/", ".").replace("\\", ".")
    if module in {"tests.test_postgres_store", "tests.test_postgres_concurrency"}:
        if collection_skip:
            return reason == "PostgreSQL driver is not installed"
        allowed = POSTGRES_CASES if module == "tests.test_postgres_store" else {"test_overlapping_scopes_on_different_tickets_cannot_both_be_claimed"}
        return name in allowed and reason == "set HOH_TEST_POSTGRES_DSN to run the PostgreSQL conformance suite"
    if module == "tests.test_store_factory":
        return name in {"test_a_url_chooses_the_server_backend",
                        "test_a_url_wins_over_a_path"} and reason == "PostgreSQL driver is not installed"
    if module == "tests.ui.test_dashboard":
        return name in UI_CASES and reason == "Enable browser acceptance with --run-ui"
    if module == "tests.test_runtime_environment":
        if name == "test_actual_different_minor_candidate_interpreter_is_isolated":
            return reason == "Set HOH_TEST_CANDIDATE_PYTHON for a cross-version installation check"
        allowed = {f"test_windows_native_exit_status_survives_gate[{code}]" for code in (1, 3221225477, 3221226505)}
        return platform != "win32" and name in allowed and reason == "Windows DWORD exit status"
    if module == "tests.test_managed_process":
        return platform != "win32" and name == "test_slow_job_attachment_cannot_lose_a_virtualenv_child" and reason == "Windows virtualenv redirector race"
    if module == "tests.test_bootstrap_local":
        if platform != "win32" and reason == "Windows PowerShell script":
            return name == "test_a_worktree_is_refused_without_creating_a_venv"
        return name == "test_a_worktree_is_refused_without_creating_a_venv" and reason == "only a checkout other than D:/HoH can show the refusal without installing" and checkout.replace("\\", "/").rstrip("/").lower() == "d:/hoh"
    if module == "tests.test_dev_scripts":
        return platform != "win32" and name in DEV_SCRIPT_CASES and reason == "the dev scripts are Windows PowerShell"
    if module == "tests.test_skill_assets":
        allowed = {f"test_project_scope_installation_copies_the_whole_tree[{role}]" for role in ("hoh-planner", "hoh-developer", "hoh-qa", "hoh-integrator")}
        return name in allowed and reason == "PowerShell is unavailable on this host"
    return False


def validate(directory: Path, expected_revision: str, lane: str) -> dict:
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    if manifest["revision"] != expected_revision or manifest["lane"] != lane:
        raise ValueError("Evidence revision or lane does not match the gated revision")
    if manifest["source_snapshot"] != source_snapshot():
        raise ValueError("Source fingerprint or dirty state changed after execution")
    source = Path(manifest["source_path"]).resolve()
    if Path(manifest["checkout"]).resolve() != ROOT or source != ROOT / "src/hoh_control_plane/__init__.py":
        raise ValueError("Source identity is outside the attested checkout")
    if manifest["exit_code"] != 0 or manifest["termination_reason"] != "passed":
        raise ValueError("The actual test process did not pass")
    report = (directory / "tests.xml").read_bytes()
    if hashlib.sha256(report).hexdigest() != manifest["junit_sha256"]:
        raise ValueError("JUnit report changed after execution")
    document = ET.fromstring(report)
    suites = list(document.iter("testsuite"))
    if not suites or any(s.get("name") != f"hoh-{expected_revision}-{lane}" for s in suites):
        raise ValueError("JUnit revision or lane does not match")
    for suite in suites:
        own_cases = list(suite.findall("testcase"))
        actual = {"tests": len(own_cases), **{tag + "s" if tag != "skipped" else tag:
                  sum(case.find(tag) is not None for case in own_cases)
                  for tag in ("failure", "error", "skipped")}}
        if any(int(suite.get(field, "-1")) != count for field, count in actual.items()):
            raise ValueError("JUnit counters disagree with actual case outcomes")
    cases = list(document.iter("testcase"))
    identities = [(case.get("classname", ""), case.get("name", "")) for case in cases]
    if len(set(identities)) != len(identities) or any(not name for _, name in identities):
        raise ValueError("Missing or duplicate test identities")
    passed, skipped = [], []
    for case in cases:
        identity = [case.get("classname", ""), case.get("name", "")]
        if case.find("failure") is not None or case.find("error") is not None:
            raise ValueError(f"Failed test evidence: {identity}")
        if case.find("skipped") is not None:
            if not allowed_skip(case, manifest["platform"], manifest["checkout"]):
                raise ValueError(f"Unexpected exclusion: {identity}")
            skipped.append({"identity": identity, "reason": case.find("skipped").get("message")})
        else:
            passed.append(identity)
    if not passed:
        raise ValueError("No tests passed")
    if sum(int(s.get("tests", "-1")) for s in suites) != len(cases):
        raise ValueError("JUnit test count disagrees with its cases")
    result = {"revision": expected_revision, "lane": lane, "passed": passed, "skipped": skipped}
    if lane == "full":
        coverage_bytes = (directory / "coverage.xml").read_bytes()
        if hashlib.sha256(coverage_bytes).hexdigest() != manifest["coverage_sha256"]:
            raise ValueError("Coverage report changed after execution")
        coverage = ET.fromstring(coverage_bytes)
        lines = int(coverage.get("lines-valid", "0"))
        branches = int(coverage.get("branches-valid", "0"))
        lines_covered = int(coverage.get("lines-covered", "0"))
        branches_covered = int(coverage.get("branches-covered", "0"))
        covered = lines_covered + branches_covered
        if lines <= 0 or branches <= 0 or not 0 <= lines_covered <= lines or not 0 <= branches_covered <= branches:
            raise ValueError("Missing or invalid branch coverage")
        percent = covered * 100 / (lines + branches)
        if not math.isfinite(percent) or percent < 80:
            raise ValueError("Full coverage is below 80 percent")
        result["coverage_percent"] = percent
    return result


def run(lane: str, directory: Path, basetemp: str, workers: str) -> int:
    # New directories prevent old reports from being mistaken for this run.
    directory.mkdir(parents=True, exist_ok=False)
    sys.path.insert(0, str(ROOT / "src"))
    import hoh_control_plane
    from hoh_control_plane.managed_process import run_managed

    actual_revision = revision()
    environment = os.environ.copy()
    for key in list(environment):
        upper = key.upper()
        if upper in {"PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTHONHOME", "PYTHONSTARTUP", "HOH_TEST_ALLOW_FOREIGN_PACKAGE"} or upper.startswith(("COV_CORE", "COVERAGE")) or (upper.startswith("HOH_") and not upper.startswith("HOH_TEST_")):
            environment.pop(key)
    environment["PYTHONPATH"] = str(ROOT / "src")
    environment["PYTHONUTF8"] = "1"
    command = [sys.executable, "-m", "pytest", "-q", "-n", workers,
               "-p", "no:cacheprovider", "-c", str(ROOT / "pyproject.toml"), f"--basetemp={basetemp}",
               f"--junitxml={directory / 'tests.xml'}", "-o",
               f"junit_suite_name=hoh-{actual_revision}-{lane}"]
    if lane == "fast":
        command += ["-m", FAST_MARKERS]
    else:
        command += ["--cov=hoh_control_plane", "--cov-branch", "--cov-report=term-missing",
                    f"--cov-report=xml:{directory / 'coverage.xml'}", "--cov-fail-under=80"]
    manifest = {"revision": actual_revision, "lane": lane, "checkout": str(ROOT),
                "source_path": str(Path(hoh_control_plane.__file__).resolve()),
                "platform": sys.platform, "python": sys.executable,
                "command": command, "started_ns": time.time_ns(),
                "source_snapshot": source_snapshot()}
    manifest_path = directory / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps({"command": command, "evidence": str(directory)}), flush=True)
    started = time.perf_counter()
    execution = run_managed(command, cwd=ROOT, env=environment,
                            timeout=1800 if lane == "fast" else 4500, output_limit=10_000_000)
    (directory / "pytest.log").write_text(execution.stdout + execution.stderr, encoding="utf-8")
    print(execution.stdout, end="")
    print(execution.stderr, end="", file=sys.stderr)
    manifest.update(wall_seconds=time.perf_counter() - started, exit_code=execution.returncode,
                    termination_reason="passed" if execution.returncode == 0 and not execution.timed_out and not execution.capture_errors and not execution.stdout_truncated and not execution.stderr_truncated else "failed")
    for filename, field in (("tests.xml", "junit_sha256"), ("coverage.xml", "coverage_sha256")):
        path = directory / filename
        if path.is_file():
            manifest[field] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    try:
        if revision() != actual_revision:
            raise ValueError("Checkout revision changed during the test run")
        result = validate(directory, actual_revision, lane)
        (directory / "inventory.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    except (ValueError, KeyError, OSError, ET.ParseError) as error:
        print(f"CI evidence rejected: {error}", file=sys.stderr)
        return execution.returncode or 1
    return execution.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("run", "validate"))
    parser.add_argument("--lane", choices=("fast", "full"), required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--basetemp")
    parser.add_argument("--workers", default="2")
    parser.add_argument("--expected-revision")
    args = parser.parse_args()
    try:
        if args.action == "run":
            if not args.basetemp:
                parser.error("run requires an explicit private --basetemp")
            return run(args.lane, args.evidence.resolve(), args.basetemp, args.workers)
        if not args.expected_revision:
            parser.error("validate requires --expected-revision")
        result = validate(args.evidence, args.expected_revision, args.lane)
        print(json.dumps(result, indent=2))
        return 0
    except (ValueError, KeyError, OSError, ET.ParseError) as error:
        print(f"CI evidence rejected: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
