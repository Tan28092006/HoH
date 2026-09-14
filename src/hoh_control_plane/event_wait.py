"""An open MCP call sleeps on commit notifications, with durable replay.

UDP carries only a local wake hint. Database state, role authorization and leases
remain authoritative. A periodic server-side recovery read costs no model turns.
"""
from __future__ import annotations

import asyncio
from datetime import timedelta
import hashlib
import json
import math
import secrets
import socket
import threading
import time

from hoh_control_plane.domain import Actor, Role, AuthorizationError, ConflictError, ValidationError
from hoh_control_plane.service._common import (
    _iso,
    _now,
    _required_execution_capabilities,
    _row,
    require_project,
)

RECOVERY_SECONDS = 20
WAIT_PRESENCE_SECONDS = 90
REPOSITORY_OBSERVE_SECONDS = 60
_REPOSITORY_OBSERVE_LOCK = threading.Lock()
_REPOSITORY_OBSERVED_AT: dict[str, float] = {}
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


async def _repository_pending(service, actor, project):
    """Observe remote Git while an idle Planner waits, without model polling."""
    if actor.role != Role.PLANNER:
        return []
    now = time.monotonic()
    if now - _REPOSITORY_OBSERVED_AT.get(project, 0) < REPOSITORY_OBSERVE_SECONDS:
        return []
    if not _REPOSITORY_OBSERVE_LOCK.acquire(blocking=False):
        return []
    failure = None
    manifest = None
    try:
        now = time.monotonic()
        if now - _REPOSITORY_OBSERVED_AT.get(project, 0) < REPOSITORY_OBSERVE_SECONDS:
            return []
        try:
            manifest = await asyncio.to_thread(service.sync_repository, actor, project)
        except Exception as error:
            failure = error
        finally:
            _REPOSITORY_OBSERVED_AT[project] = time.monotonic()
    finally:
        _REPOSITORY_OBSERVE_LOCK.release()
    if failure is not None:
        summary = str(failure)[:500]
        fingerprint = hashlib.sha256(
            f"{type(failure).__name__}:{summary}".encode()
        ).hexdigest()[:20]
        return [(f"repository-sync-failed:{project}:{fingerprint}", {
            'kind': 'repository_sync_failed',
            'error_type': type(failure).__name__,
            'summary': summary,
            'next_action': 'repair repository access before declaring the board current',
        })]
    assert manifest is not None
    if not manifest.get('changed'):
        return []
    context_ref = manifest.get('context_ref')
    key = 'repository:' + (context_ref or hashlib.sha256(json.dumps({
        'local': manifest.get('local_commit'),
        'upstream': manifest.get('upstream_commit'),
        'branches': manifest.get('branch_updates') or [],
    }, sort_keys=True).encode()).hexdigest()[:20])
    return [(key, {
        'kind': 'repository_changed',
        'relation': manifest.get('relation'),
        'local_commit': manifest.get('local_commit'),
        'upstream_commit': manifest.get('upstream_commit'),
        'document_paths': (manifest.get('document_paths') or [])[:20],
        'branch_updates': (manifest.get('branch_updates') or [])[:20],
        'context_ref': context_ref,
        'reconciliation_id': manifest.get('reconciliation_id'),
        'observed_commit_count': len(manifest.get('observed_commits') or []),
        'commit_refs': [
            item.get('commit') for item in (manifest.get('observed_commits') or [])[:20]
        ],
        'next_action': (
            'resolve the repository observation; inspect each commit and relevant '
            'Git/docs/spec diff, classify every previously-open ticket, apply lifecycle '
            'changes, then call planner.resolve_repository_reconciliation'
        ),
    })]


def _pending(service, actor, project, kind=None, identifier=None):
    if kind is not None:
        job, poll_tool, _ = _job(service, actor, project, kind, identifier)
        if job['status'] in {'QUEUED', 'RUNNING'}:
            return []
        return [(f'job:{kind}:{identifier}:{job["status"]}', {'kind': 'job_completed',
                 'job_kind': kind, 'job_id': identifier, 'status': job['status'],
                 'ticket_ref': 'ticket:' + job['ticket_id'], 'poll_tool': poll_tool})]
    with service.store.connection() as db:
        reconciliations = [
            service._repository_reconciliation(row)
            for row in db.execute(
                """
                SELECT * FROM repository_reconciliations
                WHERE project_id = ? AND status = 'PENDING'
                ORDER BY created_at ASC LIMIT 20
                """,
                (project,),
            ).fetchall()
        ]
        if reconciliations:
            first = reconciliations[0]
            payload = {
                'kind': (
                    'repository_changed' if actor.role == Role.PLANNER
                    else 'repository_reconciliation_required'
                ),
                'reconciliation_id': first['reconciliation_id'],
                'context_ref': first['observation_ref'],
                'observed_commit_count': len(first['observed_commits']),
                'commit_refs': [
                    item.get('commit') for item in first['observed_commits'][:20]
                ],
                'next_action': (
                    'resolve the observation, audit every previously-open ticket, apply '
                    'lifecycle changes, then call planner.resolve_repository_reconciliation'
                    if actor.role == Role.PLANNER else
                    'preserve the current lease/worktree and do not claim, submit or merge '
                    'until Planner resolves the repository reconciliation'
                ),
            }
            return [('repository-reconciliation:' + first['reconciliation_id'], payload)]
        presence = db.execute('SELECT capacity FROM agent_presence WHERE agent_id=?', (actor.agent_id,)).fetchone()
        table = 'planner_job_leases' if actor.role == Role.PLANNER else 'leases'
        active = db.execute(f'SELECT count(*) AS n FROM {table} WHERE agent_id=? AND expires_at>?', (actor.agent_id, _iso())).fetchone()['n']
        if active >= presence['capacity']:
            return []
        if actor.role == Role.PLANNER:
            jobs = [('planner:' + row['planner_job_id'] + ':' + row['updated_at'],
                     {'kind': 'planner_work', 'planner_job_id': row['planner_job_id'],
                      'ticket_ref': 'ticket:' + row['ticket_id']})
                    for row in db.execute("SELECT planner_job_id,ticket_id,updated_at FROM planner_jobs WHERE project_id=? AND status='READY' ORDER BY created_at LIMIT 50", (project,))]
            if jobs:
                return jobs
            qa_tickets = [
                _row(row) for row in db.execute(
                    """SELECT DISTINCT t.* FROM tickets t
                       JOIN work_queue q ON q.ticket_id = t.ticket_id
                       WHERE q.project_id = ? AND q.target_role = 'qa'
                         AND q.status IN ('pending', 'offered')
                         AND t.status = 'QA_READY'
                       ORDER BY t.priority DESC, t.ticket_id""",
                    (project,),
                ).fetchall()
            ]
            qa_agents = []
            for candidate in db.execute(
                """SELECT * FROM agent_presence
                   WHERE role = 'qa' AND expires_at > ?
                   ORDER BY updated_at DESC, agent_id""",
                (_iso(),),
            ).fetchall():
                qa = _row(candidate)
                if project not in set(qa.get('project_ids') or []):
                    continue
                qa_agents.append(qa)
            serviceable_tickets = {
                ticket['ticket_id']
                for ticket in qa_tickets
                if any(
                    service._qa_ticket_capability_state(qa, ticket)['claimable']
                    for qa in qa_agents
                )
            }
            reviews = []
            for qa in qa_agents:
                machine_id = qa.get('machine_id')
                declared = set(qa.get('execution_capabilities') or [])
                approved = set(qa.get('approved_execution_capabilities') or [])
                machine_matches = bool(
                    machine_id
                    and machine_id == qa.get('capability_approved_machine_id')
                )
                if not machine_matches:
                    approved = set()
                ticket_refs = []
                missing_declaration: set[str] = set()
                missing_approval: set[str] = set()
                for ticket in qa_tickets:
                    if ticket['ticket_id'] in serviceable_tickets:
                        continue
                    required = _required_execution_capabilities(ticket)
                    undeclared = required - declared
                    unapproved = (required & declared) - approved
                    if required and (undeclared or unapproved):
                        ticket_refs.append('ticket:' + ticket['ticket_id'])
                        missing_declaration.update(undeclared)
                        missing_approval.update(unapproved)
                if not ticket_refs:
                    continue
                fingerprint = hashlib.sha256(json.dumps({
                    'agent_id': qa['agent_id'],
                    'machine_id': machine_id,
                    'declared': sorted(declared),
                    'approved': sorted(approved),
                    'missing_declaration': sorted(missing_declaration),
                    'missing_approval': sorted(missing_approval),
                    'ticket_refs': ticket_refs,
                }, sort_keys=True).encode()).hexdigest()[:20]
                reviews.append((
                    f"capability-review:{qa['agent_id']}:{fingerprint}",
                    {
                        'kind': 'capability_review_required',
                        'agent_id': qa['agent_id'],
                        'machine_id': machine_id,
                        'declared_capabilities': sorted(declared),
                        'missing_declaration': sorted(missing_declaration),
                        'missing_approval': sorted(missing_approval),
                        'approval_machine_matches': machine_matches,
                        'ticket_refs': ticket_refs,
                        'next_action': (
                            'provision or route missing equipment and have QA verify '
                            'then re-register; approve only separately verified public evidence'
                            if missing_declaration else
                            'verify public preflight evidence, then call '
                            'planner.configure_agent_capability_profile'
                        ),
                    },
                ))
            return reviews
    inbox = service.get_agent_inbox(actor, project, limit=20)
    offers = [('queue:' + offer['queue_id'], {'kind': 'work_available',
               'ticket_ref': 'ticket:' + offer['ticket']['ticket_id'],
               'queue_id': offer['queue_id'], 'offer_id': offer['offer_id']})
              for offer in inbox['offers']]
    if offers or actor.role != Role.QA:
        return offers
    blockers = inbox.get('capability_blockers') or []
    if not blockers:
        return []
    fingerprint = hashlib.sha256(json.dumps(blockers, sort_keys=True).encode()).hexdigest()[:20]
    return [(f'capability-blocked:{actor.agent_id}:{fingerprint}', {
        'kind': 'capability_blocked',
        'blockers': blockers,
        'next_action': (
            'inspect missing_declaration and missing_approval; verify callable '
            'equipment and re-register once, then wait for Planner approval or provisioning'
        ),
    })]


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
            repository = await _repository_pending(service, actor, project_id)
            candidates = repository or _pending(
                service, actor, project_id, job_kind, job_id
            )
            result = _deliver(service, actor, project_id, candidates, wait_id)
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
