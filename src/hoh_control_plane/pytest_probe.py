"""Trusted pytest instrumentation, launched by absolute path in the candidate Python.

The original Python flags and pytest arguments are retained. This file and its
resource helper are shipped with the runner, never copied into candidate source.
"""
import hashlib
import importlib
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import types
import uuid


# Sent directly over the xdist gateway; never imported from candidate source.
_EXCEPTION_OBSERVER = '''import pytest

@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    import os
    # The worker has registered us; subprocesses cannot import this in-memory module.
    plugins = os.environ.get("PYTEST_PLUGINS", "").split(",")
    if __name__ in plugins:
        remaining = ",".join(plugin for plugin in plugins if plugin != __name__)
        if remaining:
            os.environ["PYTEST_PLUGINS"] = remaining
        else:
            os.environ.pop("PYTEST_PLUGINS", None)

@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    report.hoh_resource_failure = bool(call.excinfo and any(
        c.__name__ in {"MemoryError", "ArrowMemoryError"}
        for c in type(call.excinfo.value).__mro__))
'''


def _drop_own_directory():
    """Remove this file's directory, which Python put first on sys.path.

    The probe runs as a script, so its directory -- the installed
    hoh_control_plane package -- becomes sys.path[0]. Its subpackages would then
    shadow same-named top-level packages for the candidate: hoh_control_plane/mcp
    hid the MCP SDK's ``mcp`` and broke every test importing it.
    """
    own = Path(__file__).resolve().parent
    sys.path[:] = [entry for entry in sys.path
                   if not entry or Path(entry).resolve() != own]


def main():
    _drop_own_directory()
    report, settings_path, *arguments = sys.argv[1:]
    settings = json.loads(Path(settings_path).read_text(encoding="utf-8"))
    root = Path.cwd().resolve()
    # Match `python -m pytest`: isolated mode omits cwd, ordinary mode includes it.
    if not sys.flags.isolated:
        sys.path.insert(0, str(root))
    spec = importlib.util.spec_from_file_location("_hoh_resources", Path(__file__).with_name("resource_probe.py"))
    resources = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(resources)
    facts = {
        "executable": str(Path(sys.executable).resolve()), "python_version": platform.python_version(),
        "implementation": platform.python_implementation(), "platform": platform.system(),
        "isolated": bool(sys.flags.isolated), "phase": "starting", "provenance_status": "unverified",
        "source_modules": {}, "test_counts": {"passed": 0, "failed": 0, "skipped": 0, "collection_errors": 0},
        "resource_failures": 0, "foreign_import_errors": [],
        "resource_before": resources.resource_snapshot(),
        "dependencies": {d.metadata["Name"]: d.version for d in importlib.metadata.distributions()
                         if d.metadata.get("Name")},
    }

    def foreign_import_path(text):
        """The failing frame of an import error, when it lies outside the worktree.

        An import that fails inside the candidate's own file is the candidate's
        fault; one that fails in another checkout or an installed package is the
        environment's, and is never a reason to send the candidate back.
        """
        if "ImportError" not in text and "ModuleNotFoundError" not in text:
            return None
        import re
        paths = re.findall(r"""([A-Za-z]:[\\/][^\s"'<>:|]+?\.py|/[^\s"'<>:|]+?\.py)\b""", text)
        if not paths:
            return None
        last = Path(paths[-1])
        try:
            last.resolve().relative_to(root)
        except (OSError, ValueError):
            return str(last)
        return None

    def git_sha():
        try:
            p = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, stdin=subprocess.DEVNULL,
                capture_output=True, text=True, timeout=10, creationflags=0x08000000 if os.name == "nt" else 0)
            value = p.stdout.strip()
            return value if p.returncode == 0 and len(value) == 40 else None
        except (OSError, subprocess.TimeoutExpired):
            return None

    facts["candidate_commit"] = git_sha()

    def tracked_clean():
        try:
            result = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"],
                cwd=root, stdin=subprocess.DEVNULL, capture_output=True, timeout=10,
                creationflags=0x08000000 if os.name == "nt" else 0)
            return result.returncode == 0 and not result.stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            return False

    facts["tracked_clean_before"] = tracked_clean() if facts["candidate_commit"] else None
    facts["skipped_tests"] = []

    def save():
        target = Path(report)
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(facts, sort_keys=True), encoding="utf-8")
        os.replace(temporary, target)

    save()
    import pytest
    observer_name = "_hoh_exception_observer_" + uuid.uuid4().hex
    observer = types.ModuleType(observer_name)
    exec(compile(_EXCEPTION_OBSERVER, __file__, "exec"), observer.__dict__)

    def verify_sources():
        names = settings["modules"]
        if not names:
            facts["provenance_status"] = "not_applicable"
            return
        for name in names:
            try:
                importlib.import_module(name)
            except Exception as error:
                facts["provenance_status"] = "unresolved"
                facts["provenance_error"] = type(error).__name__
                save()
                pytest.exit("HoH: candidate source could not be resolved", returncode=4)
        for name, module in tuple(sys.modules.items()):
            if not any(name == n or name.startswith(n + ".") for n in names):
                continue
            origin = getattr(module, "__file__", None)
            if not origin:
                paths = list(getattr(module, "__path__", []))
                if paths and all(Path(p).resolve().is_relative_to(root) for p in paths):
                    continue
                facts["provenance_status"] = "unresolved"
                save()
                pytest.exit("HoH: candidate module has no verifiable source", returncode=4)
            source = Path(origin).resolve()
            facts["source_modules"][name] = {"path": str(source)}
            if not source.is_relative_to(root):
                facts["provenance_status"] = "mismatch"
                save()
                pytest.exit("HoH: imported candidate source is outside the frozen worktree", returncode=4)
        facts["provenance_status"] = "verified"
        save()

    class Probe:
        collection_failures = set()

        @pytest.hookimpl(trylast=True)
        def pytest_sessionstart(self, session):
            facts["phase"] = "session_started"
            verify_sources()

        @pytest.hookimpl(trylast=True)
        def pytest_collection_finish(self, session):
            facts["phase"] = "collection_finished"
            facts["collected"] = len(session.items)
            verify_sources()

        def pytest_collectreport(self, report):
            if report.failed:
                # Each xdist worker collects the same files. Count the failed
                # collection once, while retaining its original diagnostic.
                if report.nodeid not in self.collection_failures:
                    self.collection_failures.add(report.nodeid)
                    facts["test_counts"]["collection_errors"] += 1
                foreign = foreign_import_path(str(report.longrepr))
                if foreign and len(facts["foreign_import_errors"]) < 20:
                    facts["foreign_import_errors"].append(
                        {"nodeid": report.nodeid[:500], "path": foreign[:500]})
                save()

        @pytest.hookimpl(optionalhook=True, tryfirst=True)
        def pytest_configure_node(self, node):
            # pytest.main's plugin objects are not passed to xdist workers.
            # Preload a unique module in the worker, before its pytest config;
            # PYTEST_PLUGINS resolves that module without changing sys.path.
            bootstrap = (
                "import os, sys, types\n"
                f"name = {observer_name!r}\n"
                "if name in sys.modules: raise RuntimeError('HoH observer collision')\n"
                "module = types.ModuleType(name)\n"
                f"exec(compile({_EXCEPTION_OBSERVER!r}, {__file__!r}, 'exec'), module.__dict__)\n"
                "sys.modules[name] = module\n"
                "existing = os.environ.get('PYTEST_PLUGINS', '')\n"
                "os.environ['PYTEST_PLUGINS'] = existing + (',' if existing else '') + name\n"
                "channel.send(name)\n"
            )
            channel = node.gateway.remote_exec(bootstrap)
            if channel.receive() != observer_name:
                raise RuntimeError("HoH worker observer registration failed")
            channel.waitclose()

        def pytest_runtest_logreport(self, report):
            # This hook receives both serial and xdist controller reports;
            # makereport only runs where the test itself executes.
            if report.failed:
                facts["test_counts"]["failed"] += 1
                crash = getattr(report.longrepr, "reprcrash", None)
                exception = getattr(crash, "message", "").split(":", 1)[0].rsplit(".", 1)[-1]
                if getattr(report, "hoh_resource_failure", False) or exception in {"MemoryError", "ArrowMemoryError"}:
                    facts["resource_failures"] += 1
            elif report.skipped:
                facts["test_counts"]["skipped"] += 1
                if len(facts["skipped_tests"]) < 100:
                    facts["skipped_tests"].append({"nodeid": report.nodeid[:500], "reason": str(report.longrepr)[-1000:]})
            elif report.when == "call":
                facts["test_counts"]["passed"] += 1
            save()

        @pytest.hookimpl(trylast=True)
        def pytest_sessionfinish(self, session, exitstatus):
            if facts["provenance_status"] in {"verified", "not_applicable"}:
                verify_sources()
            facts["phase"] = "session_finished"
            facts["pytest_exit_status"] = int(exitstatus)
            facts["resource_after"] = resources.resource_snapshot()
            facts["final_commit"] = git_sha()
            facts["tracked_clean_after"] = tracked_clean() if facts["candidate_commit"] else None
            if facts["final_commit"] != facts["candidate_commit"] or (
                facts["candidate_commit"] and (not facts["tracked_clean_before"] or not facts["tracked_clean_after"])
            ):
                facts["provenance_status"] = "mismatch"
            save()

    return int(pytest.main(arguments, plugins=[Probe(), observer]))


if __name__ == "__main__":
    raise SystemExit(main())
