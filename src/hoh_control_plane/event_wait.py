"""An open MCP call sleeps on commit notifications, with durable replay.

UDP carries only a local wake hint. Database state, role authorization and leases
remain authoritative. A periodic server-side recovery read costs no model turns.
"""
from __future__ import annotations

import asyncio
from datetime import timedelta
import json
import math
import secrets
import socket
import time

from hoh_control_plane.domain import Actor, Role, AuthorizationError, ConflictError, ValidationError
from hoh_control_plane.service._common import _iso, _now, require_project

RECOVERY_SECONDS = 20
WAIT_PRESENCE_SECONDS = 90
JOBS = {
    'provision': ('worktree_provision_jobs', 'provision_id', Role.DEVELOPER, 'dev.get_worktree_provision'),
    'submission': ('candidate_submission_jobs', 'submission_id', Role.DEVELOPER, 'dev.get_candidate_submission'),
    'qa': ('qa_test_jobs', 'test_run_id', Role.QA, 'qa.get_project_test_run'),
    'merge': ('merge_jobs', 'merge_job_id', Role.INTEGRATOR, 'integrator.get_merge_candidate'),
}


def notify_waiters(connection):
    """Best effort after commit; a notifier failure can never undo a mutation."""
    try:
        rows = connection.execute("SELECT notification_port, wait_id FROM agent_event_waiters WHERE state='waiting' AND expires_at > ?", (_iso(),)).fetchall()
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
            sender.setblocking(False)
            for row in rows:
                port, nonce = row['notification_port'], row['wait_id']
                if isinstance(port, int) and 1024 <= port <= 65535 and isinstance(nonce, str) and len(nonce) == 48:
                    try:
                        sender.sendto(nonce.encode('ascii'), ('127.0.0.1', port))
                    except (OSError, UnicodeError):
                        pass
    except Exception:
        # Includes schema creation before the additive waiter tables exist.
        pass


def session_status(service, project_id):
    with service.store.connection() as db:
        rows = [dict(row) for row in db.execute('SELECT agent_id,role,state,expires_at,updated_at FROM agent_event_waiters WHERE project_id=? ORDER BY role,agent_id', (project_id,))]
    for row in rows:
        if row['expires_at'] <= _iso():
            row['state'] = 'disconnected'
    return rows


def _wake_previous(row):
    """A replaced request should promptly observe its lost notification slot."""
    if row is None or row['state'] != 'waiting':
        return
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
            sender.sendto(row['wait_id'].encode('ascii'), ('127.0.0.1', row['notification_port']))
    except (OSError, UnicodeError, OverflowError):
        pass


def _superseded():
    return {'status': 'superseded', 'event_id': None, 'event': None,
            'next_action': 'stop_this_wait; this identity resumed in a newer request'}


def _job(service, actor, project, kind, identifier):
    if kind not in JOBS:
        raise ValidationError('Unknown job kind; use provision, submission, qa or merge')
    table, key, role, poll_tool = JOBS[kind]
    if actor.role != role:
        raise AuthorizationError('This role cannot wait for that job kind')
    with service.store.connection() as db:
        row = db.execute(f'SELECT * FROM {table} WHERE {key}=?', (identifier,)).fetchone()
        if row is None or row['requested_by'] != actor.agent_id or row['project_id'] != project:
            raise AuthorizationError('Job is not owned by this agent and project')
        job = dict(row)
        lease = db.execute('SELECT * FROM leases WHERE lease_id=? AND agent_id=? AND expires_at>?',
                           (job['lease_id'], actor.agent_id, _iso())).fetchone()
    # Terminal results may have legitimately released their ticket lease.
    if job['status'] in {'QUEUED', 'RUNNING'} and lease is None:
        raise ConflictError('The job lease expired; recover through the role workflow')
    return job, poll_tool, lease


def _pending(service, actor, project, kind=None, identifier=None):
    if kind is not None:
        job, poll_tool, _ = _job(service, actor, project, kind, identifier)
        if job['status'] in {'QUEUED', 'RUNNING'}:
            return []
        return [(f'job:{kind}:{identifier}:{job["status"]}', {'kind': 'job_completed',
                 'job_kind': kind, 'job_id': identifier, 'status': job['status'],
                 'ticket_ref': 'ticket:' + job['ticket_id'], 'poll_tool': poll_tool})]
    with service.store.connection() as db:
        presence = db.execute('SELECT capacity FROM agent_presence WHERE agent_id=?', (actor.agent_id,)).fetchone()
        table = 'planner_job_leases' if actor.role == Role.PLANNER else 'leases'
        active = db.execute(f'SELECT count(*) AS n FROM {table} WHERE agent_id=? AND expires_at>?', (actor.agent_id, _iso())).fetchone()['n']
        if active >= presence['capacity']:
            return []
        if actor.role == Role.PLANNER:
            return [('planner:' + row['planner_job_id'] + ':' + row['updated_at'],
                     {'kind': 'planner_work', 'planner_job_id': row['planner_job_id'],
                      'ticket_ref': 'ticket:' + row['ticket_id']})
                    for row in db.execute("SELECT planner_job_id,ticket_id,updated_at FROM planner_jobs WHERE project_id=? AND status='READY' ORDER BY created_at LIMIT 50", (project,))]
    inbox = service.get_agent_inbox(actor, project, limit=20)
    return [('queue:' + offer['queue_id'], {'kind': 'work_available',
             'ticket_ref': 'ticket:' + offer['ticket']['ticket_id'],
             'queue_id': offer['queue_id'], 'offer_id': offer['offer_id']}) for offer in inbox['offers']]


def _reserve(db, actor, project, key):
    current = db.execute('SELECT r.agent_id,r.expires_at,w.expires_at AS connection_expiry FROM agent_event_reservations r LEFT JOIN agent_event_waiters w ON w.agent_id=r.agent_id WHERE r.project_id=? AND r.event_key=?', (project, key)).fetchone()
    if current and current['agent_id'] != actor.agent_id and current['expires_at'] > _iso() and (current['connection_expiry'] or '') > _iso():
        return False
    db.execute('DELETE FROM agent_event_reservations WHERE project_id=? AND event_key=?', (project, key))
    db.execute('INSERT INTO agent_event_reservations VALUES (?,?,?,?)',
               (project, key, actor.agent_id, _iso(_now() + timedelta(seconds=WAIT_PRESENCE_SECONDS))))
    return True


def _deliver(service, actor, project, candidates, wait_id=None):
    with service.store.connection(immediate=True) as db:
        if wait_id and not db.execute('SELECT 1 FROM agent_event_waiters WHERE agent_id=? AND wait_id=?', (actor.agent_id, wait_id)).fetchone():
            return _superseded()
        pending_rows = db.execute("SELECT event_id,event_key,payload FROM agent_event_deliveries WHERE agent_id=? AND project_id=? AND state='pending' ORDER BY created_at LIMIT 50", (actor.agent_id, project)).fetchall()
        eligible = {key for key, _ in candidates}
        for pending in pending_rows:
            payload = json.loads(pending['payload'])
            if pending['event_key'] in eligible and _reserve(db, actor, project, pending['event_key']):
                return {'status': 'event', 'event_id': pending['event_id'], 'event': payload, 'replayed': True}
            if payload['kind'] == 'work_available':
                source = db.execute('SELECT status FROM work_queue WHERE queue_id=?', (payload['queue_id'],)).fetchone()
                if source is None or source['status'] not in {'pending', 'offered'}:
                    db.execute("UPDATE agent_event_deliveries SET state='obsolete' WHERE event_id=?", (pending['event_id'],))
        for key, payload in candidates:
            exists = db.execute('SELECT 1 FROM agent_event_deliveries WHERE agent_id=? AND project_id=? AND event_key=?', (actor.agent_id, project, key)).fetchone()
            if exists or not _reserve(db, actor, project, key):
                continue
            payload = {**payload, 'project_id': project, 'role': actor.role.value}
            identifier = 'delivery_' + secrets.token_hex(16)
            db.execute("INSERT INTO agent_event_deliveries VALUES (?,?,?,?,?,'pending',?,NULL)",
                       (identifier, actor.agent_id, project, key, json.dumps(payload), _iso()))
            return {'status': 'event', 'event_id': identifier, 'event': payload, 'replayed': False}


async def wait_for_event(service, actor, project_id, *, timeout_seconds=43200,
                         ack_event_id=None, job_kind=None, job_id=None,
                         resume_wait_id=None, keepalive=None):
    require_project(actor, project_id)
    if actor.role not in {Role.PLANNER, Role.DEVELOPER, Role.QA, Role.INTEGRATOR}:
        raise AuthorizationError('Only engineering roles can wait for work')
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 43200:
        raise ValidationError('timeout_seconds must be positive and at most 43200')
    if bool(job_kind) != bool(job_id):
        raise ValidationError('job_kind and job_id must be supplied together')
    if job_kind:
        _job(service, actor, project_id, job_kind, job_id)
    receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    receiver.bind(('127.0.0.1', 0))
    receiver.setblocking(False)
    wait_id = secrets.token_hex(24)
    registered, state = False, 'disconnected'
    try:
        with service.store.connection(immediate=True) as db:
            service._refresh_presence(db, actor, WAIT_PRESENCE_SECONDS)
            # Resolve project existence before installing the foreign-key record.
            service._one(db, 'SELECT project_id FROM projects WHERE project_id=?', (project_id,))
            old = db.execute('SELECT * FROM agent_event_waiters WHERE agent_id=?', (actor.agent_id,)).fetchone()
            if resume_wait_id and (not old or old['wait_id'] != resume_wait_id or old['project_id'] != project_id or old['role'] != actor.role.value):
                raise ConflictError('The prior wait changed; refresh session.bootstrap before resuming')
            if old and old['state'] == 'waiting' and old['expires_at'] > _iso() and not resume_wait_id:
                raise ConflictError('This identity has a pending wait. For this same chat resuming, read event_wait in session.bootstrap and pass its wait_id as resume_wait_id; only a different simultaneous worker needs another instance')
            if ack_event_id:
                delivery = db.execute('SELECT agent_id,project_id,event_key FROM agent_event_deliveries WHERE event_id=?', (ack_event_id,)).fetchone()
                if not delivery or delivery['agent_id'] != actor.agent_id or delivery['project_id'] != project_id:
                    raise AuthorizationError('Cannot acknowledge another agent or project event')
                db.execute("UPDATE agent_event_deliveries SET state='acked',acknowledged_at=? WHERE event_id=?", (_iso(), ack_event_id))
                db.execute('DELETE FROM agent_event_reservations WHERE project_id=? AND event_key=? AND agent_id=?', (project_id, delivery['event_key'], actor.agent_id))
            db.execute('DELETE FROM agent_event_waiters WHERE agent_id=?', (actor.agent_id,))
            db.execute("INSERT INTO agent_event_waiters VALUES (?,?,?,?,?,'waiting',?,?)",
                       (actor.agent_id, project_id, actor.role.value, wait_id, receiver.getsockname()[1],
                        _iso(_now() + timedelta(seconds=WAIT_PRESENCE_SECONDS)), _iso()))
        registered = True
        _wake_previous(old)
        deadline = time.monotonic() + timeout_seconds
        heartbeat_at = 0
        while True:
            if time.monotonic() >= heartbeat_at:
                if keepalive is not None:
                    await keepalive()
                job = lease = None
                if job_kind:
                    job, _, lease = _job(service, actor, project_id, job_kind, job_id)
                with service.store.connection(immediate=True) as db:
                    if not db.execute('SELECT 1 FROM agent_event_waiters WHERE agent_id=? AND wait_id=?', (actor.agent_id, wait_id)).fetchone():
                        state = 'superseded'
                        return _superseded()
                    service._refresh_presence(db, actor, WAIT_PRESENCE_SECONDS)
                    if lease:
                        # Same checks as a work heartbeat, under the slot's write
                        # lock so a superseded wait cannot renew execution authority.
                        renewed = db.execute('UPDATE leases SET heartbeat_at=?,expires_at=? WHERE lease_id=? AND agent_id=? AND role=? AND expires_at>?',
                            (_iso(), _iso(_now() + timedelta(seconds=900)), job['lease_id'], actor.agent_id, actor.role.value, _iso()))
                        if renewed.rowcount != 1:
                            raise ConflictError('The job lease expired; recover through the role workflow')
                    db.execute('UPDATE agent_event_waiters SET expires_at=?,updated_at=? WHERE wait_id=?',
                               (_iso(_now() + timedelta(seconds=WAIT_PRESENCE_SECONDS)), _iso(), wait_id))
                heartbeat_at = time.monotonic() + RECOVERY_SECONDS
            result = _deliver(service, actor, project_id, _pending(service, actor, project_id, job_kind, job_id), wait_id)
            if result:
                state = 'superseded' if result['status'] == 'superseded' else 'notified'
                return result
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                state = 'timed_out'
                return {'status': 'timeout', 'event_id': None, 'event': None}
            try:
                packet = await asyncio.wait_for(asyncio.get_running_loop().sock_recv(receiver, 128), min(remaining, RECOVERY_SECONDS))
                if packet != wait_id.encode('ascii'):
                    continue
            except TimeoutError:
                pass
    finally:
        receiver.close()
        if registered:
            with service.store.connection(immediate=True) as db:
                updated = db.execute('UPDATE agent_event_waiters SET state=?,notification_port=0,updated_at=? WHERE wait_id=?', (state, _iso(), wait_id))
                if state == 'disconnected' and updated.rowcount:
                    db.execute('UPDATE agent_presence SET expires_at=? WHERE agent_id=?', (_iso(), actor.agent_id))
