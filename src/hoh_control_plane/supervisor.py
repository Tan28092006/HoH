"""Single execution-host supervisor for durable HoH worker queues.

Run multiple clients against this host, not multiple independent execution
hosts against a shared artifact root. Host failover requires explicit recovery.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, UTC
import hashlib
import json
import os
from pathlib import Path
import socket
import sys
import threading
import time
from uuid import uuid4

from hoh_control_plane.domain import Actor, Role, ConflictError
from hoh_control_plane.managed_process import run_managed
from hoh_control_plane.resource_lock import exclusive_file_lock

JOBS = {
    'qa': ('qa_test_jobs', 'test_run_id', 'execute_queued_project_test', 'fail_queued_project_test'),
    'provision': ('worktree_provision_jobs', 'provision_id', 'execute_queued_worktree_provision', 'fail_queued_worktree_provision'),
    'submission': ('candidate_submission_jobs', 'submission_id', 'execute_queued_candidate_submission', 'fail_queued_candidate_submission'),
    'merge': ('merge_jobs', 'merge_job_id', 'execute_queued_merge', 'fail_queued_merge'),
}
ADMIN = Actor('hoh-supervisor', Role.ADMIN, frozenset({'*'}))


def now():
    return datetime.now(UTC).isoformat()


def job_lock(service, kind, identifier):
    if kind not in JOBS:
        raise ValueError('Unknown worker kind')
    key = hashlib.sha256((kind + ':' + identifier).encode()).hexdigest()
    return exclusive_file_lock(service.store.artifact_root / 'worker-locks' / (key + '.lock'))


def set_paused(service, paused: bool, *, reason: str):
    if not reason.strip() or len(reason) > 1000:
        raise ValueError('An operator reason of 1-1000 characters is required')
    with service.store.connection(immediate=True) as db:
        db.execute('DELETE FROM runtime_state WHERE state_key = ?', ('paused',))
        db.execute('INSERT INTO runtime_state VALUES (?, ?, ?)', ('paused', str(int(paused)), now()))
        db.execute('INSERT INTO runtime_actions VALUES (?, ?, ?, ?)', (uuid4().hex, 'pause' if paused else 'resume', reason, now()))


def operations_status(service):
    with service.store.connection() as db:
        state = {r['state_key']: r['state_value'] for r in db.execute('SELECT * FROM runtime_state').fetchall()}
        queues = {}
        for kind, (table, _, _, _) in JOBS.items():
            queues[kind] = [dict(r) for r in db.execute(f'SELECT status, count(*) AS count, min(created_at) AS oldest FROM {table} GROUP BY status').fetchall()]
        attention = [dict(r) for r in db.execute("SELECT merge_job_id, ticket_id, error_message FROM merge_jobs WHERE status = 'RUNNING' AND error_message IS NOT NULL ORDER BY created_at LIMIT 100").fetchall()]
    heartbeat = state.get('supervisor_heartbeat')
    stale = heartbeat is None or (datetime.now(UTC) - datetime.fromisoformat(heartbeat)).total_seconds() > 15
    return {'paused': state.get('paused') == '1', 'supervisor_stale': stale,
            'supervisor_heartbeat': heartbeat, 'queues': queues, 'needs_attention': attention}


class Supervisor:
    def __init__(self, service, max_workers=4, worker_timeout=2100):
        if not 1 <= max_workers <= 32 or not 5 <= worker_timeout <= 7200:
            raise ValueError('Worker count must be 1-32 and timeout 5-7200 seconds')
        self.service = service
        self.max_workers = max_workers
        self.worker_timeout = worker_timeout
        self.runner_id = socket.gethostname() + ':' + hashlib.sha256(str(service.store.artifact_root.resolve()).encode()).hexdigest()[:16]
        self.active = {}
        self.stopping = threading.Event()
        self.pool = None
        self.lock = exclusive_file_lock(service.store.artifact_root / 'supervisor.lock')

    def __enter__(self):
        self.lock.__enter__()
        try:
            self.pool = ThreadPoolExecutor(max_workers=self.max_workers)
            self.recover()
            return self
        except BaseException:
            self.__exit__(*sys.exc_info())
            raise

    def __exit__(self, *exception):
        self.stopping.set()
        if self.pool:
            self.pool.shutdown(wait=True, cancel_futures=True)
        self.lock.__exit__(*exception)

    def _job(self, kind, identifier):
        table, key, _, _ = JOBS[kind]
        with self.service.store.connection() as db:
            row = db.execute(f'SELECT * FROM {table} WHERE {key} = ?', (identifier,)).fetchone()
        return None if row is None else dict(row)

    def _pending(self):
        # Constants define all SQL identifiers. Limit each source before sorting.
        pending = []
        with self.service.store.connection() as db:
            for kind, (table, key, _, _) in JOBS.items():
                for row in db.execute(f"SELECT {key}, created_at FROM {table} WHERE status = 'QUEUED' ORDER BY created_at LIMIT ?", (self.max_workers * 2,)).fetchall():
                    pending.append((row['created_at'], kind, row[key]))
        return [(kind, identifier) for _, kind, identifier in sorted(pending)]

    def recover(self):
        with self.service.store.connection() as db:
            interrupted = [dict(r) for r in db.execute("SELECT * FROM worker_executions WHERE runner_id = ? AND status = 'RUNNING'", (self.runner_id,)).fetchall()]
            merges = [r[0] for r in db.execute("SELECT merge_job_id FROM merge_jobs WHERE status = 'RUNNING' ORDER BY created_at LIMIT 100").fetchall()]
        for execution in interrupted:
            kind, identifier = execution['job_kind'], execution['job_id']
            try:
                with job_lock(self.service, kind, identifier):
                    job = self._job(kind, identifier)
                    if job and job['status'] == 'RUNNING':
                        if kind == 'merge':
                            self.service.reconcile_merge_job(ADMIN, identifier)
                        else:
                            getattr(self.service, JOBS[kind][3])(identifier, 'Supervisor recovered an interrupted worker; original artifacts were retained')
                    with self.service.store.connection(immediate=True) as db:
                        db.execute("UPDATE worker_executions SET status = 'INTERRUPTED', completed_at = ?, outcome = ? WHERE execution_id = ?",
                                   (now(), 'Previous supervisor stopped; durable job inspected', execution['execution_id']))
            except ConflictError:
                continue  # Child cleanup is still in progress. Do not take over it.
        for identifier in merges:
            if ('merge', identifier) not in self.active:
                self.service.reconcile_merge_job(ADMIN, identifier)

    def _submit(self, kind, identifier):
        execution_id = uuid4().hex
        with self.service.store.connection(immediate=True) as db:
            db.execute('''INSERT INTO worker_executions
                (execution_id, runner_id, job_kind, job_id, status, started_at, heartbeat_at)
                VALUES (?, ?, ?, ?, 'RUNNING', ?, ?)''', (execution_id, self.runner_id, kind, identifier, now(), now()))
        return self.pool.submit(self._execute, execution_id, kind, identifier)

    def _execute(self, execution_id, kind, identifier):
        environment = {k: v for k, v in os.environ.items() if not k.startswith('HOH_')}
        environment['PYTHONPATH'] = str(Path(__file__).resolve().parents[1])
        environment['HOH_ARTIFACT_ROOT'] = str(self.service.store.artifact_root.resolve())
        if hasattr(self.service.store, 'dsn'):
            environment['HOH_DATABASE_URL'] = self.service.store.dsn
            database = 'unused.db'
        else:
            database = str(self.service.store.path.resolve())
        next_guard = 0.0
        def revoked():
            nonlocal next_guard
            if self.stopping.is_set():
                return True
            if time.monotonic() < next_guard:
                return False
            next_guard = time.monotonic() + 1
            job = self._job(kind, identifier)
            if job is None or job['status'] not in {'QUEUED', 'RUNNING'}:
                return True
            with self.service.store.connection(immediate=True) as db:
                lease = db.execute('SELECT expires_at FROM leases WHERE lease_id = ?', (job['lease_id'],)).fetchone()
                db.execute('UPDATE worker_executions SET heartbeat_at = ? WHERE execution_id = ?', (now(), execution_id))
            return lease is None or datetime.fromisoformat(lease['expires_at'].replace('Z', '+00:00')) <= datetime.now(UTC)
        outcome = 'worker error'
        try:
            result = run_managed([sys.executable, '-m', 'hoh_control_plane.managed_worker',
                '--database', database, '--kind', kind, '--job-id', identifier],
                cwd=self.service.store.artifact_root, env=environment, timeout=self.worker_timeout, cancel=revoked)
            outcome = 'cancelled' if result.cancelled else 'timeout' if result.timed_out else f'exit {result.returncode}'
        finally:
            job = self._job(kind, identifier)
            if kind == 'merge' and job and job['status'] == 'RUNNING':
                self.service.reconcile_merge_job(ADMIN, identifier)
            elif job and job['status'] == 'RUNNING':
                getattr(self.service, JOBS[kind][3])(identifier, 'Worker stopped before publishing a result: ' + outcome)
            with self.service.store.connection(immediate=True) as db:
                db.execute("UPDATE worker_executions SET status = 'FINISHED', completed_at = ?, outcome = ? WHERE execution_id = ?",
                           (now(), outcome, execution_id))

    def tick(self):
        for key, future in list(self.active.items()):
            if future.done():
                # State survives worker errors; a failure must not kill the dispatcher.
                if future.exception() is not None:
                    print(json.dumps({'event': 'worker_error', 'kind': key[0], 'job_id': key[1], 'error_type': type(future.exception()).__name__}), flush=True)
                del self.active[key]
        with self.service.store.connection(immediate=True) as db:
            paused = db.execute("SELECT state_value FROM runtime_state WHERE state_key = 'paused'").fetchone()
            db.execute("DELETE FROM runtime_state WHERE state_key = 'supervisor_heartbeat'")
            db.execute('INSERT INTO runtime_state VALUES (?, ?, ?)', ('supervisor_heartbeat', now(), now()))
        started = 0
        if not self.stopping.is_set() and not (paused and paused[0] == '1'):
            for key in self._pending():
                if len(self.active) >= self.max_workers:
                    break
                if key not in self.active:
                    self.active[key] = self._submit(*key)
                    started += 1
        return {'started': started, 'active': len(self.active)}

    def run_until_idle(self, timeout=60):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = self.tick()
            if not result['active'] and not self._pending():
                return
            time.sleep(0.1)
        raise TimeoutError('Worker queue did not drain before the deadline')


def main():
    from hoh_control_plane.service import ControlPlaneService
    from hoh_control_plane.store_factory import store_from_environment
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--max-workers', type=int, default=4)
    parser.add_argument('--worker-timeout', type=int, default=2100)
    args = parser.parse_args()
    service = ControlPlaneService(store_from_environment())
    with Supervisor(service, args.max_workers, args.worker_timeout) as supervisor:
        while True:
            try:
                supervisor.tick()
            except Exception as error:
                # Do not leak connection credentials in a database error string.
                print(json.dumps({'event': 'supervisor_unavailable', 'error_type': type(error).__name__}), flush=True)
            time.sleep(1)


if __name__ == '__main__':
    main()
