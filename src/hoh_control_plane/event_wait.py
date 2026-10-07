"""An open MCP call sleeps on commit notifications, with durable replay.

UDP carries only a local wake hint. Database state, role authorization and leases
remain authoritative. A periodic server-side recovery read costs no model turns.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import hashlib
import json
import math
import socket
import threading
import time

from hoh_control_plane.domain import Actor, Role, AuthorizationError, ConflictError, ValidationError
from hoh_control_plane.service._common import (
    REPOSITORY_OBSERVE_SECONDS,
    _id,
    _iso,
    _now,
    _required_execution_capabilities,
    _row,
    require_project,
)

RECOVERY_SECONDS = 20
MAX_WAIT_SECONDS = 43200
# A wait requested over MCP is held for at least this long, whatever
# timeout_seconds the model chose. Observed on the live marketing project: role
# chats passed about 60 seconds, so every wait ended empty, was narrated, and was
# called again, until the chat ended its turn and the role went offline. An event
# still returns the wait at once, so a long hold costs nothing when work exists.
# Tests that need a quick timeout lower this explicitly.
MIN_MCP_WAIT_SECONDS = MAX_WAIT_SECONDS
# What an empty return tells the model, at the moment it decides what to do next.
SILENT_RESUME = (
    'No event arrived. Call work.wait_for_event again now with the same project_id '
    'and write nothing to the chat; report only an event you must act on, a '
    'superseded wait, an error, or a question only the user can answer.'
)
# A pending barrier is announced once, then again each time it crosses one of
# these ages, so a Planner that is away is chased and the roles it blocks are
# told how long they have been waiting instead of sitting silent.
BARRIER_ESCALATION_SECONDS = (1800, 7200, 28800, 86400)
WAIT_PRESENCE_SECONDS = 90
# REPOSITORY_OBSERVE_SECONDS comes from the service kernel: the scheduler's
# repository sweep observes on the same clock (project_repositories.observed_at).
# The in-process map below only keeps this process's waits from racing each other.
_REPOSITORY_OBSERVE_LOCK = threading.Lock()
_REPOSITORY_OBSERVED_AT: dict[str, float] = {}
JOBS = {
    'provision': ('worktree_provision_jobs', 'provision_id', Role.DEVELOPER, 'dev.get_worktree_provision'),
    'submission': ('candidate_submission_jobs', 'submission_id', Role.DEVELOPER, 'dev.get_candidate_submission'),
    'qa': ('qa_test_jobs', 'test_run_id', Role.QA, 'qa.get_project_test_run'),
    'merge': ('merge_jobs', 'merge_job_id', Role.INTEGRATOR, 'integrator.get_merge_candidate'),
}


def barrier_age_seconds(opened_at):
    """Whole seconds since a reconciliation was opened; 0 for an unparseable stamp."""
    try:
        opened = datetime.fromisoformat(str(opened_at).replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return 0
    return max(0, int((_now() - opened).total_seconds()))


def notify_waiters(connection):
    """Best effort after commit; a notifier failure can never undo a mutation."""
    try:
        rows = connection.execute("SELECT notification_port, wait_id FROM agent_event_waiters WHERE state='waiting' AND expires_at > ?", (_iso(),)).fetchall()
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
            sender.setblocking(False)
            for row in rows:
                port, nonce = row['notification_port'], row['wait_id']
                if isinstance(port, int) and 1024 <= port <= 65535 and isinstance(nonce, str) and 0 < len(nonce) <= 64:
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
    if not service.repository_observation_due(project):
        # Observed within the window by another process (the scheduler sweep);
        # any reconciliation it opened reaches this Planner through _pending.
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


def _expire_abandoned_planner_jobs(service):
    """Requeue Planner jobs whose lease lapsed, so the wait below can offer them.

    A read checks first: the write transaction is only taken when a lease has
    actually expired, so an idle wait loop never contends for the write lock.
    """
    with service.store.connection() as db:
        if db.execute('SELECT 1 FROM planner_job_leases WHERE expires_at <= ? LIMIT 1', (_iso(),)).fetchone() is None:
            return
    with service.store.connection(immediate=True) as db:
        service._expire_planner_job_leases(db)


PLANNER_JOB_EVENT_LIMIT = 50


_HIGH_SEVERITY_JOB = """EXISTS (
              SELECT 1 FROM planner_interventions i
              WHERE i.planner_job_id = j.planner_job_id AND i.severity = 'high'
                AND i.status IN ('PENDING', 'DELIVERED')
          )"""


def _planner_job_events(db, actor, project, *, high_only=False):
    """READY Planner jobs this Planner has not already acknowledged, urgent first.

    The window excludes, in SQL, every job whose current event this agent has
    already acknowledged (or that went obsolete), so fifty old reports read but
    not resolved can never hide a new blocker behind the LIMIT (review R3). A
    delivered but unacknowledged event stays in the window, so it is replayed.
    ``high_only`` keeps only high-severity jobs, the ones a barrier must not hide.
    """
    rows = db.execute(
        f"""
        SELECT j.planner_job_id, j.ticket_id, j.updated_at FROM planner_jobs j
        WHERE j.project_id = ? AND j.status = 'READY'
          AND NOT EXISTS (
              SELECT 1 FROM agent_event_deliveries d
              WHERE d.agent_id = ? AND d.project_id = j.project_id
                AND d.event_key = 'planner:' || j.planner_job_id || ':' || j.updated_at
                AND d.state != 'pending'
          )
          {'AND ' + _HIGH_SEVERITY_JOB if high_only else ''}
        ORDER BY
          CASE WHEN {_HIGH_SEVERITY_JOB} THEN 0 ELSE 1 END,
          j.created_at, j.planner_job_id
        LIMIT ?
        """,
        (project, actor.agent_id, PLANNER_JOB_EVENT_LIMIT),
    ).fetchall()
    return [('planner:' + row['planner_job_id'] + ':' + row['updated_at'],
             {'kind': 'planner_work', 'planner_job_id': row['planner_job_id'],
              'ticket_ref': 'ticket:' + row['ticket_id']})
            for row in rows]


def _capacity_left(db, actor):
    """True while this agent holds fewer leases than its registered capacity."""
    presence = db.execute('SELECT capacity FROM agent_presence WHERE agent_id=?', (actor.agent_id,)).fetchone()
    table = 'planner_job_leases' if actor.role == Role.PLANNER else 'leases'
    active = db.execute(f'SELECT count(*) AS n FROM {table} WHERE agent_id=? AND expires_at>?', (actor.agent_id, _iso())).fetchone()['n']
    return presence is not None and active < presence['capacity']


def _pending(service, actor, project, kind=None, identifier=None):
    if kind is not None:
        job, poll_tool, _ = _job(service, actor, project, kind, identifier)
        if job['status'] in {'QUEUED', 'RUNNING'}:
            return []
        return [(f'job:{kind}:{identifier}:{job["status"]}', {'kind': 'job_completed',
                 'job_kind': kind, 'job_id': identifier, 'status': job['status'],
                 'ticket_ref': 'ticket:' + job['ticket_id'], 'poll_tool': poll_tool})]
    if actor.role == Role.PLANNER:
        _expire_abandoned_planner_jobs(service)
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
        if actor.role != Role.PLANNER:
            # A ticket-scoped reconciliation freezes only its tickets, whose
            # offers it withdrew; an execution role keeps waiting for other work.
            reconciliations = [
                item for item in reconciliations if item['scope'] == 'project'
            ]
        if reconciliations:
            first = reconciliations[0]
            age = barrier_age_seconds(first['created_at'])
            level = sum(1 for threshold in BARRIER_ESCALATION_SECONDS if age >= threshold)
            planner = actor.role == Role.PLANNER
            payload = {
                'kind': (
                    'repository_changed' if planner
                    else 'repository_reconciliation_required' if level == 0
                    else 'planning_barrier_pending'
                ),
                'reconciliation_id': first['reconciliation_id'],
                'context_ref': first['observation_ref'],
                'observed_commit_count': len(first['observed_commits']),
                'commit_refs': [
                    item.get('commit') for item in first['observed_commits'][:20]
                ],
                'opened_at': first['created_at'],
                'age_seconds': age,
                'escalation_level': level,
                'pending_count': len(reconciliations),
                'scope': first['scope'],
                'next_action': (
                    'resolve the observation, audit every previously-open ticket, apply '
                    'lifecycle changes, then call planner.resolve_repository_reconciliation'
                    if planner else
                    'preserve the current lease/worktree and do not claim, submit or merge '
                    'until Planner resolves the repository reconciliation'
                ),
            }
            if first['scope'] != 'project':
                payload['frozen_ticket_ids'] = (first.get('frozen_ticket_ids') or [])[:20]
                if planner:
                    # A ticket-scoped barrier froze only these tickets; the
                    # rest of the board keeps working and needs no audit.
                    payload['next_action'] = (
                        'resolve the observation, audit only the frozen tickets ('
                        + (', '.join(payload['frozen_ticket_ids']) or 'none listed')
                        + '), apply lifecycle changes, then call '
                        'planner.resolve_repository_reconciliation'
                    )
            if level:
                payload['escalation'] = 'barrier_stale'
                payload['next_action'] = (
                    f'this barrier has blocked execution for {age // 60} minutes; '
                    + payload['next_action']
                    + ('' if planner else '; if no Planner chat is open, ask the operator to resume one')
                )
            key = 'repository-reconciliation:' + first['reconciliation_id']
            if level:
                key += f':stale{level}'
            events = [(key, payload)]
            if planner and _capacity_left(db, actor):
                # A barrier is the Planner's first duty, but it must not hide a
                # high-severity blocker: once the barrier event is acknowledged,
                # the next wait delivers the urgent job (review A6).
                events.extend(_planner_job_events(db, actor, project, high_only=True))
            return events
        if not _capacity_left(db, actor):
            return []
        if actor.role == Role.PLANNER:
            jobs = _planner_job_events(db, actor, project)
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
                evidence_refs, proven = service.latest_capability_preflight_refs(
                    db, project, qa['agent_id'], machine_id
                )
                fingerprint = hashlib.sha256(json.dumps({
                    'agent_id': qa['agent_id'],
                    'machine_id': machine_id,
                    'evidence_refs': evidence_refs,
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
                        'declared_capabilities': sorted(declared),
                        'missing_declaration': sorted(missing_declaration),
                        'missing_approval': sorted(missing_approval),
                        'approval_machine_matches': machine_matches,
                        'evidence_refs': evidence_refs,
                        'proven_capabilities': proven,
                        'ticket_refs': ticket_refs,
                        'next_action': (
                            'provision or route missing equipment and have QA verify '
                            'then re-register'
                            if missing_declaration else
                            'call planner.configure_agent_capability_profile with the '
                            'proven capabilities and these evidence_refs'
                            if evidence_refs and missing_approval <= set(proven) else
                            'the host preflight cannot prove every missing capability; '
                            'provision it or configure planner.configure_capability_preflight, '
                            'then QA reruns qa.run_capability_preflight'
                            if evidence_refs else
                            'no host preflight yet; QA is told to run qa.run_capability_preflight '
                            'and HoH opens your approval job from it'
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


def _with_dispatcher_advice(service, actor, result, job_kind):
    """Tell a dispatcher chat that has claimed too many tickets to restart.

    Only an idle wait carries it: a wait on the agent's own job is mid-ticket.
    Advisory; the server never refuses work because of it.
    """
    if job_kind or result.get('status') == 'superseded':
        return result
    advice = service.dispatcher_advice(actor)
    return {**result, **advice} if advice else result


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
            identifier = _id(db, 'delivery')
            db.execute("INSERT INTO agent_event_deliveries VALUES (?,?,?,?,?,'pending',?,NULL)",
                       (identifier, actor.agent_id, project, key, json.dumps(payload), _iso()))
            return {'status': 'event', 'event_id': identifier, 'event': payload, 'replayed': False}


async def wait_for_event(service, actor, project_id, *, timeout_seconds=43200,
                         ack_event_id=None, job_kind=None, job_id=None,
                         resume_wait_id=None, keepalive=None):
    require_project(actor, project_id)
    if actor.role not in {Role.PLANNER, Role.DEVELOPER, Role.QA, Role.INTEGRATOR}:
        raise AuthorizationError('Only engineering roles can wait for work')
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= MAX_WAIT_SECONDS:
        raise ValidationError(f'timeout_seconds must be positive and at most {MAX_WAIT_SECONDS}')
    if bool(job_kind) != bool(job_id):
        raise ValidationError('job_kind and job_id must be supplied together')
    if job_kind:
        _job(service, actor, project_id, job_kind, job_id)
    receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    receiver.bind(('127.0.0.1', 0))
    receiver.setblocking(False)
    wait_id = ''
    registered, state = False, 'disconnected'
    try:
        with service.store.connection(immediate=True) as db:
            # Allocated under the same write lock as the waiter row it names;
            # the authenticated identity, not the id's length, is what protects
            # a resume.
            wait_id = _id(db, 'wait')
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
                if actor.role == Role.PLANNER and delivery['event_key'].startswith('planner:'):
                    # planner:<job id>:<updated_at>; a delivery report closes on ack.
                    service._close_acknowledged_delivery_report(
                        db, actor, delivery['event_key'].split(':', 2)[1])
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
                return _with_dispatcher_advice(service, actor, result, job_kind)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                state = 'timed_out'
                return _with_dispatcher_advice(service, actor, {
                    'status': 'timeout', 'event_id': None, 'event': None,
                    'next_action': SILENT_RESUME}, job_kind)
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
