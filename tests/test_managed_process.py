"""run_managed owns the whole process tree it starts.

The negative assertions used to be clock-based: a child slept 1.5 s before
writing a marker and the test slept 1.6 s before checking that the marker was
absent. On a loaded machine an escaped child that merely started late had not
written the marker yet, so the test passed for the wrong reason (review F2).
Now each child records its PID as soon as it runs and then sleeps for a minute,
and the test asserts that this PID dies. An escaped child would still be alive
when the deadline passes; a slow machine only makes the test wait longer.
"""
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

# A process that escaped the tree outlives every deadline below by far.
_CHILD_LIFETIME = 60
# How long a terminated process may take to disappear on a loaded machine.
_DEATH_DEADLINE = 15


def _child_code(pid_file: Path) -> str:
    """A child that records its PID atomically, then lives for a minute."""
    return (
        "import os,pathlib,time;"
        f"p=pathlib.Path({str(pid_file)!r});t=p.with_suffix('.tmp');"
        "t.write_text(str(os.getpid()));os.replace(t,p);"
        f"time.sleep({_CHILD_LIFETIME})"
    )


def _wait_for_parent_code(pid_file: Path) -> str:
    """Parent code that blocks until its child has recorded its PID."""
    return (
        "import pathlib,time;"
        f"p=pathlib.Path({str(pid_file)!r});d=time.monotonic()+30;"
        "exec('while not p.exists() and time.monotonic()<d: time.sleep(0.02)')"
    )


def _pid_alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A zombie is dead for our purpose; reap it if it is ours.
    try:
        reaped, _status = os.waitpid(pid, os.WNOHANG)
        return reaped == 0
    except ChildProcessError:
        return True


def _assert_dies(pid_file: Path, why: str) -> None:
    assert pid_file.exists(), "The child never started, so this run proves nothing"
    pid = int(pid_file.read_text())
    deadline = time.monotonic() + _DEATH_DEADLINE
    while _pid_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    alive = _pid_alive(pid)
    if alive:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    assert not alive, why


def test_timeout_stops_descendants_and_bounds_output(tmp_path):
    from hoh_control_plane.managed_process import run_managed
    pid_file = tmp_path / 'child.pid'
    child = _child_code(pid_file)
    parent = (
        f"import subprocess,sys,time;subprocess.Popen([sys.executable,'-c',{child!r}]);"
        f"{_wait_for_parent_code(pid_file)};print('x'*1000000,flush=True);time.sleep({_CHILD_LIFETIME})"
    )
    # Long enough for both interpreters to start on a loaded host; the parent
    # then sleeps far past it, so the run can only end by timing out.
    result = run_managed([sys.executable, '-c', parent], cwd=tmp_path, timeout=8, output_limit=1024)
    assert result.timed_out
    assert len(result.stdout) < 1200
    _assert_dies(pid_file, 'A timeout must terminate descendants, not just their parent')


def test_success_does_not_leave_background_children(tmp_path):
    from hoh_control_plane.managed_process import run_managed
    pid_file = tmp_path / 'child.pid'
    child = _child_code(pid_file)
    parent = (
        f"import subprocess,sys;subprocess.Popen([sys.executable,'-c',{child!r}]);"
        f"{_wait_for_parent_code(pid_file)};print('done')"
    )
    result = run_managed([sys.executable, '-c', parent], cwd=tmp_path, timeout=30)
    assert result.returncode == 0 and 'done' in result.stdout
    _assert_dies(pid_file, 'A finished command must not leave a background child running')


def test_cancellation_and_lost_authorization_fail_closed(tmp_path):
    from hoh_control_plane.managed_process import run_managed
    def broken_guard():
        raise ConnectionError('database unavailable')
    result = run_managed([sys.executable, '-c', 'import time;time.sleep(30)'], cwd=tmp_path, timeout=10, cancel=broken_guard)
    assert result.cancelled
    assert not result.timed_out


def test_owner_crash_terminates_the_owned_process_tree(tmp_path):
    # Import first: absence of the implementation is an intentional RED signal.
    from hoh_control_plane import managed_process  # noqa: F401
    pid_file = tmp_path / 'child.pid'
    child = _child_code(pid_file)
    owner = (
        "from hoh_control_plane.managed_process import run_managed;import sys;"
        f"run_managed([sys.executable,'-c',{child!r}],cwd={str(tmp_path)!r},timeout={_CHILD_LIFETIME + 30})"
    )
    process = subprocess.Popen([sys.executable, '-c', owner], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 30
        while not pid_file.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert pid_file.exists(), 'Child must actually start before the owner is killed'
        process.kill()
        process.wait(timeout=5)
        _assert_dies(pid_file, 'Owner death must not leave its child running')
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)


@pytest.mark.skipif(os.name != 'nt', reason='Windows virtualenv redirector race')
def test_slow_job_attachment_cannot_lose_a_virtualenv_child(tmp_path, monkeypatch):
    from hoh_control_plane import managed_process
    pid_file = tmp_path / 'interpreter.pid'
    original_job = managed_process._WindowsJob
    def delayed_attach(process):
        # A Windows venv launcher may spawn the actual Python interpreter
        # before the caller gets a chance to attach the launcher to its job.
        time.sleep(.3)
        return original_job(process)
    monkeypatch.setattr(managed_process, '_WindowsJob', delayed_attach)
    command = [sys.executable, '-c', _child_code(pid_file)]
    result = managed_process.run_managed(command, cwd=tmp_path, timeout=.1)
    assert result.timed_out
    # The interpreter may have been stopped before it could record its PID; an
    # escaped one records it eventually, however slowly it starts.
    deadline = time.monotonic() + _DEATH_DEADLINE
    while not pid_file.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    if pid_file.exists():
        _assert_dies(pid_file, 'The venv redirector spawned an unowned interpreter before job attachment')
