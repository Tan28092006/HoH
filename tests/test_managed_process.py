import os
from pathlib import Path
import subprocess
import sys
import time

import pytest


def test_timeout_stops_descendants_and_bounds_output(tmp_path):
    from hoh_control_plane.managed_process import run_managed
    marker = tmp_path / 'escaped.txt'
    child = f"import time,pathlib; time.sleep(1.5); pathlib.Path({str(marker)!r}).write_text('escaped')"
    parent = f"import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',{child!r}]); print('x'*1000000,flush=True); time.sleep(10)"
    result = run_managed([sys.executable, '-c', parent], cwd=tmp_path, timeout=0.6, output_limit=1024)
    assert result.timed_out
    assert len(result.stdout) < 1200
    time.sleep(1.6)
    assert not marker.exists(), 'A timeout must terminate descendants, not just their parent'


def test_success_does_not_leave_background_children(tmp_path):
    from hoh_control_plane.managed_process import run_managed
    marker = tmp_path / 'escaped.txt'
    child = f"import time,pathlib; time.sleep(1.5); pathlib.Path({str(marker)!r}).write_text('escaped')"
    parent = f"import subprocess,sys; subprocess.Popen([sys.executable,'-c',{child!r}]); print('done')"
    result = run_managed([sys.executable, '-c', parent], cwd=tmp_path, timeout=5)
    assert result.returncode == 0 and 'done' in result.stdout
    time.sleep(1.6)
    assert not marker.exists()


def test_cancellation_and_lost_authorization_fail_closed(tmp_path):
    from hoh_control_plane.managed_process import run_managed
    def broken_guard():
        raise ConnectionError('database unavailable')
    result = run_managed([sys.executable, '-c', 'import time;time.sleep(30)'], cwd=tmp_path, timeout=10, cancel=broken_guard)
    assert result.cancelled
    assert not result.timed_out


def test_owner_crash_terminates_the_owned_process_tree(tmp_path):
    # Import first: absence of the implementation is an intentional RED signal.
    from hoh_control_plane import managed_process
    ready, escaped = tmp_path / 'ready', tmp_path / 'escaped'
    child = f"import pathlib,time;pathlib.Path({str(ready)!r}).touch();time.sleep(2);pathlib.Path({str(escaped)!r}).touch()"
    owner = f"from hoh_control_plane.managed_process import run_managed;import sys;run_managed([sys.executable,'-c',{child!r}],cwd={str(tmp_path)!r},timeout=20)"
    process = subprocess.Popen([sys.executable, '-c', owner], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 8
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert ready.exists(), 'Child must actually start before the owner is killed'
        process.kill()
        process.wait(timeout=5)
        time.sleep(2.2)
        assert not escaped.exists(), 'Owner death must not leave its child running'
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)


@pytest.mark.skipif(os.name != 'nt', reason='Windows virtualenv redirector race')
def test_slow_job_attachment_cannot_lose_a_virtualenv_child(tmp_path, monkeypatch):
    from hoh_control_plane import managed_process
    escaped = tmp_path / 'escaped-after-timeout'
    original_job = managed_process._WindowsJob
    def delayed_attach(process):
        # A Windows venv launcher may spawn the actual Python interpreter
        # before the caller gets a chance to attach the launcher to its job.
        time.sleep(.3)
        return original_job(process)
    monkeypatch.setattr(managed_process, '_WindowsJob', delayed_attach)
    command = [sys.executable, '-c', f"import time,pathlib;time.sleep(.5);pathlib.Path({str(escaped)!r}).touch()"]
    result = managed_process.run_managed(command, cwd=tmp_path, timeout=.1)
    assert result.timed_out
    time.sleep(.6)
    assert not escaped.exists(), 'The venv redirector spawned an unowned interpreter before job attachment'
