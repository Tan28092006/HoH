"""Evidence gates must reject reports that native pytest success alone permits."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("ci_evidence", ROOT / "scripts/verify-ci-evidence.py")
evidence = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evidence)
REVISION = "a" * 40


def report(tmp_path, *, lane="fast", cases=None, coverage=None):
    document = ET.Element("testsuites")
    cases = cases if cases is not None else [("tests.test_api", "test_health", None)]
    suite = ET.SubElement(document, "testsuite", name=f"hoh-{REVISION}-{lane}", tests=str(len(cases)))
    for module, name, outcome in cases:
        case = ET.SubElement(suite, "testcase", classname=module, name=name)
        if outcome:
            tag, reason = outcome
            ET.SubElement(case, tag, type="pytest.skip" if tag == "skipped" else tag, message=reason)
    for field, tag in (("failures", "failure"), ("errors", "error"), ("skipped", "skipped")):
        suite.set(field, str(sum(outcome is not None and outcome[0] == tag for _, _, outcome in cases)))
    xml = ET.tostring(document)
    (tmp_path / "tests.xml").write_bytes(xml)
    manifest = {"revision": REVISION, "lane": lane, "checkout": str(ROOT),
                "source_path": str(ROOT / "src/hoh_control_plane/__init__.py"),
                "platform": "win32", "exit_code": 0, "termination_reason": "passed",
                "source_snapshot": evidence.source_snapshot(),
                "junit_sha256": hashlib.sha256(xml).hexdigest()}
    if coverage is not None:
        data = ET.tostring(ET.Element("coverage", coverage))
        (tmp_path / "coverage.xml").write_bytes(data)
        manifest["coverage_sha256"] = hashlib.sha256(data).hexdigest()
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return manifest


def change_manifest(tmp_path, **changes):
    path = tmp_path / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest.update(changes)
    path.write_text(json.dumps(manifest), encoding="utf-8")


def test_pass_inventory_records_explicit_prerequisite_exclusions(tmp_path):
    reason = "Enable browser acceptance with --run-ui"
    report(tmp_path, cases=[("tests.test_api", "test_health", None),
                            ("tests.ui.test_dashboard", "test_empty_project_and_error_are_observable", ("skipped", reason))])
    result = evidence.validate(tmp_path, REVISION, "fast")
    assert result["passed"] == [["tests.test_api", "test_health"]]
    assert result["skipped"] == [{"identity": ["tests.ui.test_dashboard", "test_empty_project_and_error_are_observable"], "reason": reason}]


@pytest.mark.parametrize("outcome", ["failure", "error"])
def test_native_success_cannot_hide_a_failed_case(tmp_path, outcome):
    report(tmp_path, cases=[("tests.test_api", "test_broken", (outcome, "broken"))])
    with pytest.raises(ValueError, match="Failed test evidence"):
        evidence.validate(tmp_path, REVISION, "fast")


@pytest.mark.parametrize("cases", [[], [("tests.ui.test_dashboard", "test_empty_project_and_error_are_observable", ("skipped", "Enable browser acceptance with --run-ui"))]])
def test_empty_and_all_skipped_reports_cannot_pass(tmp_path, cases):
    report(tmp_path, cases=cases)
    with pytest.raises(ValueError, match="No tests passed"):
        evidence.validate(tmp_path, REVISION, "fast")


@pytest.mark.parametrize("changes,message", [
    ({"revision": "b" * 40}, "revision or lane"),
    ({"lane": "full"}, "revision or lane"),
    ({"source_path": str(ROOT.parent / "other/src/hoh_control_plane/__init__.py")}, "Source identity"),
    ({"checkout": str(ROOT.parent / "other")}, "Source identity"),
    ({"exit_code": 1}, "process did not pass"),
    ({"termination_reason": "cancelled"}, "process did not pass"),
    ({"junit_sha256": "0" * 64}, "changed after execution"),
    ({"source_snapshot": {"files_sha256": "0" * 64, "dirty_status": ""}}, "fingerprint"),
])
def test_revision_source_native_exit_and_report_digest_are_checked(tmp_path, changes, message):
    report(tmp_path)
    change_manifest(tmp_path, **changes)
    with pytest.raises(ValueError, match=message):
        evidence.validate(tmp_path, REVISION, "fast")


@pytest.mark.parametrize("wrong_suite", ["hoh-other-fast", f"hoh-{REVISION}-full"])
def test_raw_junit_must_name_its_revision_and_lane(tmp_path, wrong_suite):
    report(tmp_path)
    path = tmp_path / "tests.xml"
    document = ET.fromstring(path.read_bytes())
    document.find("testsuite").set("name", wrong_suite)
    raw = ET.tostring(document)
    path.write_bytes(raw)
    change_manifest(tmp_path, junit_sha256=hashlib.sha256(raw).hexdigest())
    with pytest.raises(ValueError, match="JUnit revision or lane"):
        evidence.validate(tmp_path, REVISION, "fast")


def test_skip_reason_alone_does_not_allow_dropping_a_security_test(tmp_path):
    report(tmp_path, cases=[("tests.test_api", "test_health", None),
                            ("tests.test_auth", "test_unauthorized_write", ("skipped", "Enable browser acceptance with --run-ui"))])
    with pytest.raises(ValueError, match="Unexpected exclusion"):
        evidence.validate(tmp_path, REVISION, "fast")


def test_duplicate_identities_cannot_inflate_pass_count(tmp_path):
    report(tmp_path, cases=[("tests.test_api", "test_health", None)] * 2)
    with pytest.raises(ValueError, match="duplicate test identities"):
        evidence.validate(tmp_path, REVISION, "fast")


@pytest.mark.parametrize("corruption", ["missing", "malformed", "tests", "failures", "errors", "skipped"])
def test_missing_malformed_and_inconsistent_reports_fail(tmp_path, corruption):
    report(tmp_path)
    path = tmp_path / "tests.xml"
    if corruption == "missing":
        path.unlink()
    elif corruption == "malformed":
        path.write_bytes(b"<broken")
        change_manifest(tmp_path, junit_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    else:
        document = ET.fromstring(path.read_bytes())
        document.find("testsuite").set(corruption, "20")
        path.write_bytes(ET.tostring(document))
        change_manifest(tmp_path, junit_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    with pytest.raises((ValueError, OSError, ET.ParseError)):
        evidence.validate(tmp_path, REVISION, "fast")


@pytest.mark.parametrize("covered,branches,accepted", [(80, 10, True), (79, 10, False), (90, 0, False)])
def test_full_coverage_requires_real_branches_and_the_eighty_percent_boundary(tmp_path, covered, branches, accepted):
    report(tmp_path, lane="full", coverage={"lines-valid": "90", "branches-valid": str(branches),
           "lines-covered": str(covered - branches), "branches-covered": str(branches)})
    if accepted:
        assert evidence.validate(tmp_path, REVISION, "full")["coverage_percent"] == 80
    else:
        with pytest.raises(ValueError, match="coverage"):
            evidence.validate(tmp_path, REVISION, "full")


@pytest.mark.parametrize("platform,accepted", [("linux", True), ("win32", False)])
def test_windows_only_skip_cannot_hide_the_contract_on_windows(platform, accepted):
    case = ET.Element("testcase", classname="tests.test_dev_scripts", name="test_the_real_database_is_refused[D:\\HoH\\data\\control-plane.db-start-dashboard.ps1]")
    ET.SubElement(case, "skipped", type="pytest.skip", message="the dev scripts are Windows PowerShell")
    assert evidence.allowed_skip(case, platform, str(ROOT)) is accepted


@pytest.mark.parametrize("module,reason", [
    ("tests.test_postgres_store", "set HOH_TEST_POSTGRES_DSN to run the PostgreSQL conformance suite"),
    ("tests.ui.test_dashboard", "Enable browser acceptance with --run-ui"),
    ("tests.test_dev_scripts", "the dev scripts are Windows PowerShell"),
    ("tests.test_runtime_environment", "Windows DWORD exit status"),
    ("tests.test_skill_assets", "PowerShell is unavailable on this host"),
])
def test_new_skips_do_not_inherit_a_module_or_parameter_family_permission(module, reason):
    case = ET.Element("testcase", classname=module, name="test_new_security_contract[unapproved]")
    ET.SubElement(case, "skipped", type="pytest.skip", message=reason)
    assert evidence.allowed_skip(case, "linux", str(ROOT)) is False


@pytest.mark.parametrize("counts", [(70, 20), (110, -20), (-10, 90)])
def test_coverage_components_cannot_compensate_for_each_others_invalid_counts(tmp_path, counts):
    report(tmp_path, lane="full", coverage={"lines-valid": "90", "branches-valid": "10",
           "lines-covered": str(counts[0]), "branches-covered": str(counts[1])})
    with pytest.raises(ValueError, match="invalid branch coverage"):
        evidence.validate(tmp_path, REVISION, "full")


def test_source_fingerprint_changes_when_actual_owned_source_bytes_change(tmp_path, monkeypatch):
    (tmp_path / "sample.py").write_text("VALUE = 1\n")
    monkeypatch.setattr(evidence, "ROOT", tmp_path)
    monkeypatch.setattr(evidence.subprocess, "check_output", lambda command, **kwargs:
        b"sample.py\0" if command[1] == "ls-files" else b"")
    original = evidence.source_snapshot()
    (tmp_path / "sample.py").write_text("VALUE = 2\n")
    changed = evidence.source_snapshot()
    assert changed["files_sha256"] != original["files_sha256"]
    assert changed["dirty_status"] == original["dirty_status"] == ""


@pytest.mark.parametrize("module", ["tests.test_postgres_store", "tests.test_postgres_concurrency"])
def test_missing_postgres_driver_collection_skip_has_an_exact_location_and_reason(module):
    case = ET.Element("testcase", classname="", name=module)
    skipped = ET.SubElement(case, "skipped", message="collection skipped")
    skipped.text = repr((str(ROOT / "tests/helpers/postgres.py"), 12, "Skipped: PostgreSQL driver is not installed"))
    assert evidence.allowed_skip(case, "linux", str(ROOT)) is True
    skipped.text = repr((str(ROOT / "tests/test_auth.py"), 12, "Skipped: PostgreSQL driver is not installed"))
    assert evidence.allowed_skip(case, "linux", str(ROOT)) is False


def test_collection_skip_text_cannot_authorize_a_different_module():
    case = ET.Element("testcase", classname="", name="tests.test_auth")
    skipped = ET.SubElement(case, "skipped", message="collection skipped")
    skipped.text = repr((str(ROOT / "tests/helpers/postgres.py"), 12, "Skipped: PostgreSQL driver is not installed"))
    assert evidence.allowed_skip(case, "linux", str(ROOT)) is False
