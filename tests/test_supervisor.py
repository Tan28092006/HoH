from concurrent.futures import Future
import os
from pathlib import Path
import sys
import subprocess
import signal
import time

from hoh_control_plane.domain import Actor, Role
from test_qa_runner import _candidate_waiting_for_qa


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


def test_supervised_mcp_does_not_launch_unbounded_detached_workers(monkeypatch, tmp_path):
    from hoh_control_plane import mcp_server
    monkeypatch.setenv('HOH_WORKER_MODE', 'supervised')
    monkeypatch.setattr(mcp_server.subprocess, 'Popen', lambda *a, **kw: (_ for _ in ()).throw(AssertionError('Unbounded spawn')))
    mcp_server._start_qa_test_worker(tmp_path / 'unused.db', 'testrun_queued')


def test_maximum_concurrency_is_enforced_before_spawn(service, actors, seeded, tmp_path, monkeypatch):
    from hoh_control_plane.supervisor import Supervisor
    job = make_job(service, actors, seeded, tmp_path)
    with service.store.connection(immediate=True) as db:
        # Distinct valid jobs use a second candidate only in real workflows.
        # Here retain the lease, but create queue records to test scheduler capacity.
        original = dict(db.execute('SELECT * FROM qa_test_jobs WHERE test_run_id = ?', (job['test_run_id'],)).fetchone())
        db.execute("UPDATE qa_test_jobs SET status = 'COMPLETED' WHERE test_run_id = ?", (job['test_run_id'],))
    with Supervisor(service, max_workers=2) as supervisor:
        monkeypatch.setattr(supervisor, '_pending', lambda: [('qa', f'testrun_capacity_{i}') for i in range(8)])
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
    command = [sys.executable, '-c', f"from pathlib import Path;import time;Path({str(ready)!r}).touch();time.sleep(4);Path({str(escaped)!r}).touch()"]
    job = make_job(service, actors, seeded, tmp_path, command)
    with Supervisor(service, max_workers=1) as supervisor:
        supervisor.tick()
        deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert ready.exists()
        with service.store.connection(immediate=True) as db:
            db.execute("UPDATE leases SET expires_at = '2000-01-01T00:00:00Z' WHERE lease_id = ?", (job['lease_id'],))
        supervisor.run_until_idle(timeout=10)
    assert not escaped.exists()
    with service.store.connection() as db:
        assert service._qa_test_job(db, job['test_run_id'])['status'] == 'FAILED'


def test_supervisor_crash_is_recovered_from_durable_execution_record(service, actors, seeded, tmp_path):
    from hoh_control_plane.supervisor import Supervisor
    ready, escaped = tmp_path / 'ready', tmp_path / 'escaped'
    command = [sys.executable, '-c', f"from pathlib import Path;import time;Path({str(ready)!r}).touch();time.sleep(4);Path({str(escaped)!r}).touch()"]
    job = make_job(service, actors, seeded, tmp_path, command)
    environment = {key: value for key, value in os.environ.items() if not key.startswith('HOH_')}
    environment['HOH_DATABASE_PATH'] = str(service.store.path)
    process = subprocess.Popen([sys.executable, '-m', 'hoh_control_plane.supervisor', '--max-workers', '1'],
                               env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert ready.exists()
        with service.store.connection() as db:
            owner_pid = int(db.execute("SELECT state_value FROM runtime_state WHERE state_key = 'supervisor_pid'").fetchone()[0])
        os.kill(owner_pid, signal.SIGTERM)
        process.wait(timeout=5)
        from contextlib import ExitStack
        from hoh_control_plane.domain import ConflictError
        with ExitStack() as stack:
            deadline = time.monotonic() + 5
            while True:
                try:
                    supervisor = stack.enter_context(Supervisor(service, max_workers=1))
                    break
                except ConflictError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.05)
            deadline = time.monotonic() + 8
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
        process.wait(timeout=5)


def test_operations_check_reports_missing_supervisor(service):
    from hoh_control_plane.operations import check_health
    report = check_health(service)
    assert report['healthy'] is False
    assert 'supervisor_stale' in report['alerts']
