"""Narrow compiler/dispatch regressions and real managed-controller barriers."""
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
import os
from pathlib import Path
import shutil
import socket
import sys
import sysconfig
import threading

import pytest

from helpers.qa import candidate_waiting_for_qa_run
from hoh_control_plane.qa_runner import ProjectTestRunner
from hoh_control_plane.supervisor import Supervisor


@pytest.mark.parametrize("code", [r"print('\N{LATIN CAPITAL LETTER A}')", r"print('\x41')",
                                  "print('é')", "# coding: unicode_escape\nprint('A')",
                                  "print(" + "9" * 700 + ")", "print('" + "A" * 1100 + "')"])
def test_unproved_literal_compilation_is_unknown(tmp_path, code):
    runner = ProjectTestRunner(tmp_path / "reports")
    assert runner.execution_environment_digest([sys.executable, "-c", code], 30) is None


def test_small_literal_keeps_matching_actual_capture(tmp_path):
    runner = ProjectTestRunner(tmp_path / "reports")
    command = [sys.executable, "-c", "print(42)"]
    key = runner.execution_environment_digest(command, 30)
    result = runner.execute(command, str(tmp_path), 30)
    assert key and result.environment_manifest["execution_environment_digest"] == key
    assert result.exit_code == 0 and result.termination_reason == "passed" and result.stdout.strip() == "42"


@pytest.mark.skipif(os.name != "nt", reason="Private copied-runtime layout is Windows-specific")
def test_compiler_dependency_loss_cannot_reuse_old_green(service, actors, seeded, tmp_path):
    runtime = tmp_path / "runtime"
    extensions = runtime / "extensions"
    extensions.mkdir(parents=True)
    base = Path(getattr(sys, "_base_executable", sys.executable)).resolve().parent
    python = runtime / "python.exe"
    shutil.copy2(base / "python.exe", python)
    for original in base.glob("*.dll"):
        shutil.copy2(original, runtime / original.name)
    for original in (base / "DLLs").iterdir():
        if original.is_file() and original.suffix.lower() in {".pyd", ".dll"}:
            shutil.copy2(original, extensions / original.name)
    stdlib = Path(sysconfig.get_path("stdlib"))
    paths = str(stdlib.resolve()) + "\n" + str(extensions) + "\n"
    for name in ("python._pth", f"python{sys.version_info.major}{sys.version_info.minor}._pth"):
        (runtime / name).write_text(paths, encoding="utf-8")
    # Mirror frozen-module source labels for the old capture, while all shared
    # stdlib reads remain read-only. Extensions below are private byte copies.
    for logical in ("abc.py", "codecs.py", "genericpath.py", "io.py", "ntpath.py", "posixpath.py", "os.py",
                    "_collections_abc.py", "stat.py", "zipimport.py", "importlib/_bootstrap.py", "importlib/_bootstrap_external.py"):
        target = runtime / "Lib" / logical
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(stdlib / logical, target)
    command = [str(python), "-c", r"print('\N{LATIN CAPITAL LETTER A}')"]
    t, submitted, owner = candidate_waiting_for_qa_run(service, actors, seeded, tmp_path, command)
    args = (actors["qa"], t["ticket_id"], owner["lease"]["lease_id"])
    first = service.queue_project_tests(*args)["test_run"]
    passed = service.execute_queued_project_test(first["test_run_id"])
    assert passed["exit_code"] == 0 and passed["termination_reason"] == "passed"
    dependency = extensions / "unicodedata.pyd"
    assert dependency.is_file()
    dependency.rename(dependency.with_suffix(".pyd.retained"))
    failed = service.project_tests.execute(command, str(tmp_path), 120)
    assert failed.exit_code != 0 and "SyntaxError" in failed.stderr
    assert service._reusable_qa_run(submitted["candidate"]["candidate_id"], command, 120) is None
    again = service.queue_project_tests(*args)
    assert not again.get("reused") and again["test_run"]["test_run_id"] != first["test_run_id"]
    assert first["execution_environment_digest"] is failed.environment_manifest["execution_environment_digest"] is None


@pytest.mark.parametrize("code,expected", [("pass", ""),
    ("import sys; assert sys.argv[1:] == ['-m', 'pytest']; print('actual-inline')", "actual-inline")])
def test_inline_trailing_argv_does_not_select_pytest(tmp_path, code, expected):
    runner = ProjectTestRunner(tmp_path / "reports")
    result = runner.execute([sys.executable, "-c", code, "-m", "pytest"], str(tmp_path), 30)
    assert result.exit_code == 0 and result.termination_reason == "passed"
    assert result.stdout.strip() == expected
    assert not result.environment_manifest.get("instrumentation")
    assert "child" not in result.environment_manifest


def test_python_script_argv_is_not_a_module_selector(tmp_path):
    script = tmp_path / "program.py"
    script.write_text("import sys\nassert sys.argv[1:] == ['-m','pytest']\nprint('actual-script')\n", encoding="utf-8")
    result = ProjectTestRunner(tmp_path / "reports").execute([sys.executable, str(script), "-m", "pytest"], str(tmp_path), 30)
    assert result.exit_code == 0 and result.termination_reason == "passed"
    assert result.stdout.strip() == "actual-script"
    assert not result.environment_manifest.get("instrumentation")


def test_real_pytest_mode_keeps_flags_and_arguments():
    assert ProjectTestRunner._pytest_arguments([sys.executable, "-X", "dev", "-m", "pytest", "-q", "-c", "private.ini"]) == (
        ["-X", "dev"], ["-q", "-c", "private.ini"])
    assert ProjectTestRunner._pytest_arguments([sys.executable, "-B", "-mpytest", "-q"]) == (["-B"], ["-q"])
    assert ProjectTestRunner._pytest_arguments([sys.executable, "-cpass", "-m", "pytest"]) is None
    assert ProjectTestRunner._pytest_arguments([sys.executable, "-W", "-m", "-c", "pass"]) is None


@pytest.mark.parametrize("control", ["deadline", "stop", "lease", "candidate", "good", "unavailable"])
def test_real_managed_loop_remains_effective_while_worker_capture_blocks(service, actors, seeded, tmp_path, monkeypatch, control):
    command = [sys.executable, "-c", "print('passed')"]
    t, _, owner = candidate_waiting_for_qa_run(service, actors, seeded, tmp_path, command)
    run = service.queue_project_tests(actors["qa"], t["ticket_id"], owner["lease"]["lease_id"])["test_run"]
    run_id = run["test_run_id"]
    capture_entered, release_capture, guard_entered = threading.Event(), threading.Event(), threading.Event()
    capture = service._capture_qa_environment
    def blocked(candidate_id):
        capture_entered.set()
        assert release_capture.wait(8), "All owned capture barriers must be released"
        result = capture(candidate_id)
        return {**result, "digest": None} if control == "unavailable" else result
    monkeypatch.setattr(service, "_capture_qa_environment", blocked)
    clock = [0.0]
    monkeypatch.setattr("hoh_control_plane.managed_process.time.monotonic", lambda: clock[0])
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(5)
    child = [sys.executable, "-I", "-S", "-c",
        f"import socket; s=socket.create_connection({listener.getsockname()!r}); s.sendall(b'ready'); s.recv(1)"]
    connection = None
    finished_before_release = True
    with Supervisor(service, max_workers=1, worker_timeout=5) as supervisor:
        original_job = supervisor._job
        def observed_job(*args):
            result = original_job(*args)
            guard_entered.set()
            return result
        monkeypatch.setattr(supervisor, "_job", observed_job)
        monkeypatch.setattr(supervisor, "_worker_command", lambda *a: child)
        with ThreadPoolExecutor(max_workers=1) as worker:
            capturing = worker.submit(service.execute_queued_project_test, run_id)
            assert capture_entered.wait(3)
            managing = supervisor._submit("qa", run_id)
            try:
                connection, _ = listener.accept()
                connection.settimeout(5)
                assert connection.recv(5) == b"ready"
                assert guard_entered.wait(3)
                # This real lifecycle write must complete while capture is held.
                service.create_feature(actors["planner"], "project-1", "independent", "Independent", "Writer control")
                if control == "deadline":
                    clock[0] = 10.0
                elif control == "stop":
                    supervisor.stopping.set()
                elif control in {"lease", "candidate"}:
                    with service.store.connection(immediate=True) as db:
                        if control == "lease":
                            db.execute("UPDATE leases SET expires_at='2000-01-01T00:00:00Z' WHERE lease_id=?", (owner["lease"]["lease_id"],))
                        else:
                            db.execute("UPDATE tickets SET candidate_commit=? WHERE ticket_id=?", ("e" * 40, t["ticket_id"]))
                    clock[0] = 2.0
                if control in {"deadline", "stop", "lease", "candidate"}:
                    try:
                        managing.result(timeout=2)
                    except FutureTimeout:
                        finished_before_release = False
                else:
                    release_capture.set()
                    result = capturing.result(timeout=10)
                    assert result["status"] == ("FAILED" if control == "unavailable" else "COMPLETED")
            finally:
                release_capture.set()
                if connection is not None:
                    try:
                        connection.sendall(b'x')
                    except OSError:
                        pass  # A deadline/stop already reaped this owned child.
                    connection.close()
                listener.close()
                capturing.result(timeout=10)
                managing.result(timeout=10)
    assert finished_before_release, "External capture occupied the real managed cancellation/deadline loop"
    with service.store.connection() as db:
        execution = db.execute("SELECT outcome,status FROM worker_executions WHERE job_id=?", (run_id,)).fetchone()
        assert execution["status"] == "FINISHED"
        if control in {"deadline", "stop", "lease", "candidate"}:
            assert execution["outcome"] == ("timeout" if control == "deadline" else "cancelled")
        elif control == "good":
            assert execution["outcome"] == "exit 0"
        assert db.execute("SELECT COUNT(*) FROM qa_test_runs").fetchone()[0] == int(control == "good")
