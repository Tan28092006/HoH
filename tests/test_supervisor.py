from concurrent.futures import Future
import os
from pathlib import Path

import pytest


def test_serializes_memory_heavy_jobs_but_keeps_git_worker_capacity(service, monkeypatch):
    from concurrent.futures import Future
    from hoh_control_plane.supervisor import Supervisor
    with Supervisor(service, max_workers=3) as supervisor:
        monkeypatch.setattr(supervisor, "_pending", lambda: [("qa", "q1"), ("qa", "q2"), ("merge", "m1"), ("provision", "p1")])
        submitted = []
        def submit(kind, identifier):
            submitted.append((kind, identifier))
            return Future()
        monkeypatch.setattr(supervisor, "_submit", submit)
        supervisor.tick()
        assert submitted == [("qa", "q1"), ("provision", "p1")]
        # Fake futures have no work to drain at fixture teardown.
        for future in supervisor.active.values():
            future.set_result(None)
import sys
import subprocess
import signal
import time

from hoh_control_plane.domain import Actor, Role
from helpers.qa import candidate_waiting_for_qa_run as _candidate_waiting_for_qa


def make_job(service, actors, seeded, tmp_path, command=None):
    ticket, _, claim = _candidate_waiting_for_qa(service, actors, seeded, tmp_path,
        command or [sys.executable, '-c', "from pathlib import Path;Path('qa-only.txt').write_text('isolated')"])
    return service.queue_project_tests(actors['qa'], ticket['ticket_id'], claim['lease']['lease_id'])['test_run']


def test_supervisor_executes_durable_queue_without_touching_developer_tree(service, actors, seeded, tmp_path):
    from hoh_control_plane.supervisor import Supervisor
    job = make_job(service, actors, seeded, tmp_path)
    with Supervisor(service, max_workers=1) as supervisor:
        supervisor.run_until_idle(timeout=30)
    with service.store.connection() as db:
        assert service._qa_test_job(db, job['test_run_id'])['status'] == 'COMPLETED'
        developer = db.execute('SELECT worktree_path FROM worktrees WHERE ticket_id = ?', (job['ticket_id'],)).fetchone()[0]
    assert not (Path(developer) / 'qa-only.txt').exists()


def test_paused_supervisor_keeps_queued_work_durable(service, actors, seeded, tmp_path):
    from hoh_control_plane.supervisor import Supervisor, set_paused
    job = make_job(service, actors, seeded, tmp_path)
    set_paused(service, True, reason='operator maintenance')
    with Supervisor(service, max_workers=1) as supervisor:
        assert supervisor.tick()['started'] == 0
        set_paused(service, False, reason='maintenance finished')
        supervisor.run_until_idle(timeout=30)
    with service.store.connection() as db:
        assert service._qa_test_job(db, job['test_run_id'])['status'] == 'COMPLETED'


def test_pause_during_queue_scan_prevents_a_late_worker_start(service, monkeypatch):
    from hoh_control_plane.supervisor import Supervisor, set_paused
    with Supervisor(service, max_workers=1) as supervisor:
        def pending_after_operator_pause():
            set_paused(service, True, reason='desktop drain during queue scan')
            return [('qa', 'queued-before-shutdown')]
        monkeypatch.setattr(supervisor, '_pending', pending_after_operator_pause)
        monkeypatch.setattr(supervisor, '_execute', lambda *args: None)
        assert supervisor.tick()['started'] == 0
        assert not supervisor.active
        with service.store.connection() as db:
            assert db.execute('SELECT count(*) FROM worker_executions').fetchone()[0] == 0


def test_supervised_mcp_does_not_launch_unbounded_detached_workers(monkeypatch, tmp_path):
    from hoh_control_plane import mcp_server
    monkeypatch.setenv('HOH_WORKER_MODE', 'supervised')
    monkeypatch.setattr(mcp_server.subprocess, 'Popen', lambda *a, **kw: (_ for _ in ()).throw(AssertionError('Unbounded spawn')))
    mcp_server._start_qa_test_worker(tmp_path / 'unused.db', 'testrun_queued')


@pytest.mark.parametrize('unset', [True, False])
def test_supervised_is_the_default_worker_mode(monkeypatch, tmp_path, unset):
    """With no explicit mode, MCP only queues: no lock-less, timeout-less spawn."""
    from hoh_control_plane import mcp_server
    from hoh_control_plane.mcp import workers
    if unset:
        monkeypatch.delenv('HOH_WORKER_MODE', raising=False)
    else:
        monkeypatch.setenv('HOH_WORKER_MODE', '')
    monkeypatch.setattr(mcp_server.subprocess, 'Popen', lambda *a, **kw: (_ for _ in ()).throw(AssertionError('Unbounded spawn')))
    assert workers.DEFAULT_WORKER_MODE == 'supervised'
    assert workers._supervised_workers()
    for launcher in ('_start_qa_test_worker', '_start_worktree_provision_worker',
                     '_start_candidate_submission_worker', '_start_merge_worker'):
        getattr(mcp_server, launcher)(tmp_path / 'unused.db', 'job_queued')


def test_legacy_worker_mode_is_an_explicit_opt_in(monkeypatch, tmp_path):
    from hoh_control_plane import mcp_server
    monkeypatch.setenv('HOH_WORKER_MODE', 'legacy')
    launched = []
    monkeypatch.setattr(mcp_server.subprocess, 'Popen', lambda command, **kw: launched.append(command))
    mcp_server._start_qa_test_worker(tmp_path / 'unused.db', 'testrun_queued')
    assert launched and launched[0][2] == 'hoh_control_plane.qa_worker'


def test_maximum_concurrency_is_enforced_before_spawn(service, actors, seeded, tmp_path, monkeypatch):
    from hoh_control_plane.supervisor import Supervisor
    job = make_job(service, actors, seeded, tmp_path)
    with service.store.connection(immediate=True) as db:
        # Distinct valid jobs use a second candidate only in real workflows.
        # Here retain the lease, but create queue records to test scheduler capacity.
        original = dict(db.execute('SELECT * FROM qa_test_jobs WHERE test_run_id = ?', (job['test_run_id'],)).fetchone())
        db.execute("UPDATE qa_test_jobs SET status = 'COMPLETED' WHERE test_run_id = ?", (job['test_run_id'],))
    with Supervisor(service, max_workers=2) as supervisor:
        monkeypatch.setattr(supervisor, '_pending', lambda: [('provision', f'worktree_capacity_{i}') for i in range(8)])
        launches = []
        monkeypatch.setattr(supervisor, '_submit', lambda kind, identifier: launches.append(identifier) or Future())
        assert supervisor.tick()['started'] == 2
        assert supervisor.tick()['started'] == 0
        for future in supervisor.active.values():
            future.set_result(None)
        assert len(launches) == 2


def test_second_supervisor_cannot_own_the_same_execution_root(service):
    from hoh_control_plane.supervisor import Supervisor
    from hoh_control_plane.domain import ConflictError
    import pytest
    with Supervisor(service):
        with pytest.raises(ConflictError):
            with Supervisor(service):
                pytest.fail('Two supervisors owned the same execution root')


def test_failed_worker_start_does_not_respawn_forever(service, actors, seeded, tmp_path, monkeypatch):
    from hoh_control_plane import supervisor as runtime
    job = make_job(service, actors, seeded, tmp_path)
    monkeypatch.setattr(runtime, 'run_managed', lambda *a, **kw: (_ for _ in ()).throw(OSError('cannot start worker')))
    with runtime.Supervisor(service, max_workers=1) as supervisor:
        try:
            supervisor.run_until_idle(timeout=1)
        except TimeoutError:
            pass
    with service.store.connection() as db:
        assert service._qa_test_job(db, job['test_run_id'])['status'] == 'FAILED'
        assert db.execute('SELECT count(*) FROM worker_executions').fetchone()[0] == 1


def test_revoked_lease_stops_running_qa(service, actors, seeded, tmp_path):
    from hoh_control_plane.supervisor import Supervisor
    ready, escaped = tmp_path / 'ready', tmp_path / 'escaped'
    command = [sys.executable, '-c', f"from pathlib import Path;import time;Path({str(ready)!r}).touch();time.sleep(600);Path({str(escaped)!r}).touch()"]
    job = make_job(service, actors, seeded, tmp_path, command)
    with Supervisor(service, max_workers=1) as supervisor:
        supervisor.tick()
        deadline = time.monotonic() + 120
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert ready.exists()
        with service.store.connection(immediate=True) as db:
            db.execute("UPDATE leases SET expires_at = '2000-01-01T00:00:00Z' WHERE lease_id = ?", (job['lease_id'],))
        supervisor.run_until_idle(timeout=120)
    assert not escaped.exists()
    with service.store.connection() as db:
        assert service._qa_test_job(db, job['test_run_id'])['status'] == 'FAILED'


def _wait_for_exit(pid: int, timeout: float) -> bool:
    """Block until process ``pid`` has exited, or ``timeout`` seconds pass."""
    if os.name == 'nt':
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x00100000 | 0x1000, False, pid)  # SYNCHRONIZE | QUERY_LIMITED
        if not handle:
            return True  # no such process any more
        try:
            return kernel32.WaitForSingleObject(handle, int(timeout * 1000)) == 0
        finally:
            kernel32.CloseHandle(handle)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


def test_supervisor_crash_is_recovered_from_durable_execution_record(service, actors, seeded, tmp_path):
    """Killing the supervisor kills its job's process tree; a new one recovers the job.

    No step depends on how fast the machine is. The test child would sleep far
    longer than any wait here, so "escaped" can only appear if the child was not
    killed; the child reports its pid, and the test waits for that process to
    exit instead of assuming it has. Every deadline below only bounds how long a
    real failure takes to report.
    """
    from hoh_control_plane.supervisor import Supervisor
    ready, escaped = tmp_path / 'ready', tmp_path / 'escaped'
    staged = tmp_path / 'ready.tmp'
    command = [sys.executable, '-c', (
        "import os,time;from pathlib import Path;"
        f"Path({str(staged)!r}).write_text(str(os.getpid()));"
        f"Path({str(staged)!r}).replace({str(ready)!r});"
        f"time.sleep(600);Path({str(escaped)!r}).touch()"
    )]
    job = make_job(service, actors, seeded, tmp_path, command)
    environment = {key: value for key, value in os.environ.items() if not key.startswith('HOH_')}
    environment['HOH_DATABASE_PATH'] = str(service.store.path)
    process = subprocess.Popen([sys.executable, '-m', 'hoh_control_plane.supervisor', '--max-workers', '1'],
                               env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    child_pid = None
    try:
        deadline = time.monotonic() + 120
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert ready.exists(), 'the supervised test command never started'
        child_pid = int(ready.read_text())
        with service.store.connection() as db:
            owner_pid = int(db.execute("SELECT state_value FROM runtime_state WHERE state_key = 'supervisor_pid'").fetchone()[0])
        os.kill(owner_pid, signal.SIGTERM)
        process.wait(timeout=60)
        assert _wait_for_exit(child_pid, 120), 'the job process outlived its supervisor'
        from contextlib import ExitStack
        from hoh_control_plane.domain import ConflictError
        with ExitStack() as stack:
            deadline = time.monotonic() + 60
            while True:
                try:
                    supervisor = stack.enter_context(Supervisor(service, max_workers=1))
                    break
                except ConflictError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.05)
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                supervisor.recover()
                with service.store.connection() as db:
                    if service._qa_test_job(db, job['test_run_id'])['status'] == 'FAILED':
                        break
                time.sleep(0.1)
            else:
                raise AssertionError('Interrupted job was not recovered')
        assert not escaped.exists()
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=60)
        if child_pid is not None and not _wait_for_exit(child_pid, 0):
            try:
                os.kill(child_pid, signal.SIGTERM)
            except OSError:
                pass


def test_operations_check_reports_missing_supervisor(service):
    from hoh_control_plane.operations import check_health
    report = check_health(service)
    assert report['healthy'] is False
    assert 'supervisor_stale' in report['alerts']


def test_another_host_cannot_consume_local_artifact_jobs(service):
    from hoh_control_plane.supervisor import Supervisor
    from hoh_control_plane.domain import ConflictError
    import pytest
    with service.store.connection(immediate=True) as db:
        db.execute('INSERT INTO runtime_state VALUES (?, ?, ?)', ('execution_host', 'another-host:other-filesystem', '2026-01-01T00:00:00+00:00'))
    with pytest.raises(ConflictError, match='host'):
        with Supervisor(service):
            pytest.fail('A second host must not silently consume jobs with local paths')


def test_worker_mode_typo_cannot_fall_back_to_unbounded_execution(monkeypatch, tmp_path):
    from hoh_control_plane import mcp_server
    from hoh_control_plane.domain import ValidationError
    import pytest
    monkeypatch.setenv('HOH_WORKER_MODE', 'supevised')
    monkeypatch.setattr(mcp_server.subprocess, 'Popen', lambda *a, **kw: None)
    with pytest.raises(ValidationError, match='HOH_WORKER_MODE'):
        mcp_server._start_qa_test_worker(tmp_path / 'unused.db', 'testrun_queued')
