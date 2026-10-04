import asyncio
import json
import socket
import time

import pytest

from hoh_control_plane.domain import Actor, Role, AuthorizationError, ConflictError


def register(service, actor):
    service.register_agent(actor, skills=[actor.role.value], capacity=1)


@pytest.fixture(autouse=True)
def planned_ticket(service, seeded):
    with service.store.connection(immediate=True) as db:
        db.execute("UPDATE tickets SET status='BACKLOG' WHERE ticket_id='DEV-142'")
        db.execute("UPDATE work_queue SET status='cancelled' WHERE ticket_id='DEV-142'")


def release(service, actors):
    service.set_ticket_ready(actors['planner'], 'DEV-142', expected_version=1)


def test_wait_delivers_committed_work_without_model_polling(service, actors, seeded):
    from hoh_control_plane.event_wait import wait_for_event
    register(service, actors['developer'])
    async def scenario():
        waiting = asyncio.create_task(wait_for_event(service, actors['developer'], 'project-1', timeout_seconds=4))
        await asyncio.sleep(.1)
        assert not waiting.done()
        started = time.monotonic()
        release(service, actors)
        result = await waiting
        assert time.monotonic() - started < 1
        assert result['event']['ticket_ref'] == 'ticket:DEV-142'
        assert result['event']['kind'] == 'work_available'
        assert result['event']['project_id'] == 'project-1'
        assert result['event_id']
    asyncio.run(scenario())


def test_delivery_replays_after_disconnect_until_acknowledged(service, actors, seeded):
    from hoh_control_plane.event_wait import wait_for_event
    register(service, actors['developer'])
    release(service, actors)
    async def scenario():
        first = await wait_for_event(service, actors['developer'], 'project-1', timeout_seconds=1)
        replay = await wait_for_event(service, actors['developer'], 'project-1', timeout_seconds=1)
        assert replay['event_id'] == first['event_id']
        empty = await wait_for_event(service, actors['developer'], 'project-1', timeout_seconds=.15,
                                     ack_event_id=first['event_id'])
        assert empty['status'] == 'timeout'
    asyncio.run(scenario())


def test_concurrent_wait_same_identity_is_rejected_and_cancel_cleans_up(service, actors, seeded):
    from hoh_control_plane.event_wait import wait_for_event, session_status
    register(service, actors['developer'])
    async def scenario():
        first = asyncio.create_task(wait_for_event(service, actors['developer'], 'project-1', timeout_seconds=3))
        await asyncio.sleep(.05)
        with pytest.raises(ConflictError):
            await wait_for_event(service, actors['developer'], 'project-1', timeout_seconds=1)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert session_status(service, 'project-1')[0]['state'] == 'disconnected'
    asyncio.run(scenario())


def test_other_project_and_role_do_not_wake_a_waiter(service, actors, seeded):
    from hoh_control_plane.event_wait import wait_for_event
    register(service, actors['qa'])
    release(service, actors)
    async def scenario():
        with pytest.raises(AuthorizationError):
            await wait_for_event(service, actors['qa'], 'private-project', timeout_seconds=1)
        result = await wait_for_event(service, actors['qa'], 'project-1', timeout_seconds=.1)
        assert result['status'] == 'timeout'
    asyncio.run(scenario())


def test_busy_agent_does_not_hold_work_away_from_idle_peer(service, actors, seeded):
    from hoh_control_plane.event_wait import wait_for_event
    from datetime import UTC, datetime, timedelta
    for name in ('developer', 'developer_2'):
        register(service, actors[name])
    release(service, actors)
    service.create_ticket(actors['planner'], 'payment-retry', 'elsewhere', 'Other work', 'Busy fixture',
                          Role.DEVELOPER, ['Done'], [], ['other/**'])
    now = datetime.now(UTC)
    with service.store.connection(immediate=True) as db:
        # Same actor is occupied in another project too: capacity is global.
        db.execute("INSERT INTO leases VALUES ('busy','elsewhere',?,'developer',?,?,?)",
                   (actors['developer'].agent_id, now.isoformat(), now.isoformat(), (now + timedelta(minutes=5)).isoformat()))
    async def scenario():
        busy = await wait_for_event(service, actors['developer'], 'project-1', timeout_seconds=.1)
        idle = await wait_for_event(service, actors['developer_2'], 'project-1', timeout_seconds=1)
        assert busy['status'] == 'timeout'
        assert idle['event']['ticket_ref'] == 'ticket:DEV-142'
    asyncio.run(scenario())


def test_rolled_back_event_never_signals_or_delivers(service, actors, seeded):
    from hoh_control_plane.event_wait import wait_for_event
    register(service, actors['developer'])
    async def scenario():
        waiting = asyncio.create_task(wait_for_event(service, actors['developer'], 'project-1', timeout_seconds=.25))
        await asyncio.sleep(.05)
        with pytest.raises(RuntimeError):
            with service.store.connection(immediate=True) as db:
                db.execute("INSERT INTO audit_events (event_id,event_type,aggregate_type,aggregate_id,aggregate_version,project_id,actor_id,actor_role,payload,occurred_at) VALUES ('rollback','test','ticket','DEV-142',1,'project-1','a','planner','{}','now')")
                raise RuntimeError('rollback')
        assert (await waiting)['status'] == 'timeout'
    asyncio.run(scenario())


def test_mcp_surface_exposes_async_wait_for_all_four_roles(service, actors):
    from hoh_control_plane.mcp_server import create_mcp_server
    async def check():
        for name in ('planner', 'developer', 'qa', 'integrator'):
            server = create_mcp_server(service, actors[name])
            tools = {tool.name: tool for tool in await server.list_tools()}
            assert 'work.wait_for_event' in tools
            assert tools['work.wait_for_event'].inputSchema['properties']['timeout_seconds']['default'] == 43200
    asyncio.run(check())


def test_completed_job_wakes_only_its_owner(service, actors, seeded):
    from hoh_control_plane.event_wait import wait_for_event
    from datetime import UTC, datetime, timedelta
    register(service, actors['developer'])
    now = datetime.now(UTC)
    with service.store.connection(immediate=True) as db:
        db.execute("INSERT INTO leases VALUES ('owned','DEV-142',?,'developer',?,?,?)",
                   (actors['developer'].agent_id, now.isoformat(), now.isoformat(), (now + timedelta(minutes=5)).isoformat()))
        db.execute("INSERT INTO worktree_provision_jobs (provision_id,project_id,ticket_id,lease_id,expected_version,requested_by,status,created_at) VALUES ('provision_test','project-1','DEV-142','owned',1,?,'QUEUED',?)",
                   (actors['developer'].agent_id, now.isoformat()))
    async def check():
        with pytest.raises(AuthorizationError):
            await wait_for_event(service, actors['developer_2'], 'project-1', job_kind='provision', job_id='provision_test', timeout_seconds=1)
        waiting = asyncio.create_task(wait_for_event(service, actors['developer'], 'project-1', job_kind='provision', job_id='provision_test', timeout_seconds=3))
        await asyncio.sleep(.05)
        assert not waiting.done()
        with service.store.connection(immediate=True) as db:
            db.execute("UPDATE worktree_provision_jobs SET status='COMPLETED' WHERE provision_id='provision_test'")
        result = await waiting
        assert result['event']['kind'] == 'job_completed'
        assert result['event']['poll_tool'] == 'dev.get_worktree_provision'
    asyncio.run(check())


def test_consumed_offer_does_not_replay_stale_work_to_another_session(service, actors, seeded):
    from hoh_control_plane.event_wait import wait_for_event
    for name in ('developer', 'developer_2'):
        register(service, actors[name])
    release(service, actors)
    async def check():
        await wait_for_event(service, actors['developer'], 'project-1', timeout_seconds=1)
        service.claim_ticket(actors['developer_2'], ticket_id='DEV-142', expected_version=2, lease_seconds=900)
        result = await wait_for_event(service, actors['developer'], 'project-1', timeout_seconds=.1)
        assert result['status'] == 'timeout'
    asyncio.run(check())


@pytest.mark.parametrize('timeout', [0, -1, 43201, True, float('nan'), float('inf'), '1'])
def test_invalid_wait_limits_are_rejected(service, actors, timeout):
    from hoh_control_plane.event_wait import wait_for_event
    from hoh_control_plane.domain import ValidationError
    with pytest.raises(ValidationError):
        asyncio.run(wait_for_event(service, actors['developer'], 'project-1', timeout_seconds=timeout))


def test_wrong_ack_and_partial_job_reference_are_rejected(service, actors):
    from hoh_control_plane.event_wait import wait_for_event
    from hoh_control_plane.domain import ValidationError
    register(service, actors['developer'])
    async def check():
        with pytest.raises(AuthorizationError):
            await wait_for_event(service, actors['developer'], 'project-1', ack_event_id='not-owned', timeout_seconds=1)
        with pytest.raises(ValidationError):
            await wait_for_event(service, actors['developer'], 'project-1', job_kind='qa', timeout_seconds=1)
        with pytest.raises(AuthorizationError):
            await wait_for_event(service, actors['developer'], 'project-1', job_kind='qa', job_id='private', timeout_seconds=1)
    asyncio.run(check())


def test_notification_failure_keeps_committed_work_and_recovery_finds_it(service, actors, monkeypatch):
    import hoh_control_plane.event_wait as events
    register(service, actors['developer'])
    monkeypatch.setattr(events, 'notify_waiters', lambda connection: None)
    monkeypatch.setattr(events, 'RECOVERY_SECONDS', .05)
    async def check():
        waiting = asyncio.create_task(events.wait_for_event(service, actors['developer'], 'project-1', timeout_seconds=1))
        await asyncio.sleep(.1)
        release(service, actors)
        assert (await waiting)['event']['ticket_ref'] == 'ticket:DEV-142'
    asyncio.run(check())


def test_separate_publisher_process_wakes_open_mcp_session(service, actors, tmp_path):
    import os
    import sys
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    async def check():
        environment = {**os.environ, 'HOH_DATABASE_PATH': str(service.store.path),
                       'HOH_AGENT_ROLE': 'developer', 'HOH_AGENT_ID': actors['developer'].agent_id,
                       'HOH_PROJECT_IDS': 'project-1', 'HOH_MCP_TRANSPORT': 'stdio'}
        environment.pop('HOH_DATABASE_URL', None)
        async with stdio_client(StdioServerParameters(command=sys.executable, args=['-m','hoh_control_plane.mcp_server'], env=environment)) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()
                result = await client.call_tool('agent.register', {'skills': ['hoh-developer']})
                assert not result.isError
                waiting = asyncio.create_task(client.call_tool('work.wait_for_event', {'project_id':'project-1','timeout_seconds':5}))
                await asyncio.sleep(.3)
                assert not waiting.done()
                started = time.monotonic()
                release(service, actors)
                result = await waiting
                assert not result.isError, result
                assert result.structuredContent['event']['ticket_ref'] == 'ticket:DEV-142'
                assert time.monotonic() - started < 1.5
    asyncio.run(check())


def test_one_ticket_wakes_one_idle_agent_instead_of_racing_models(service, actors):
    from hoh_control_plane.event_wait import wait_for_event
    for name in ('developer', 'developer_2'):
        register(service, actors[name])
    release(service, actors)
    async def check():
        first = await wait_for_event(service, actors['developer'], 'project-1', timeout_seconds=.1)
        second = await wait_for_event(service, actors['developer_2'], 'project-1', timeout_seconds=.1)
        assert first['status'] == 'event'
        assert second['status'] == 'timeout'
        with service.store.connection(immediate=True) as db:
            db.execute("UPDATE agent_event_waiters SET expires_at='2000-01-01' WHERE agent_id=?", (actors['developer'].agent_id,))
        rerouted = await wait_for_event(service, actors['developer_2'], 'project-1', timeout_seconds=.1)
        assert rerouted['status'] == 'event'
        returned = await wait_for_event(service, actors['developer'], 'project-1', timeout_seconds=.1)
        assert returned['status'] == 'timeout', 'Reconnecting must not revive an old reservation'
    asyncio.run(check())


def test_planner_receives_new_intervention_job(service, actors, monkeypatch):
    from hoh_control_plane import event_wait
    from hoh_control_plane.event_wait import wait_for_event
    # A Planner wait also observes remote Git; this project has no repository, so
    # mark it as just observed to keep the test on job delivery alone.
    monkeypatch.setitem(event_wait._REPOSITORY_OBSERVED_AT, 'project-1', time.monotonic())
    register(service, actors['planner'])
    async def check():
        waiting = asyncio.create_task(wait_for_event(service, actors['planner'], 'project-1', timeout_seconds=2))
        await asyncio.sleep(.05)
        with service.store.connection(immediate=True) as db:
            db.execute("INSERT INTO planner_jobs (planner_job_id,project_id,ticket_id,job_type,status,summary,evidence_refs,created_by,created_at,updated_at) VALUES ('plan-event','project-1','DEV-142','developer_blocker','READY','Clarify requirement','[]','dev-01','now','now')")
        result = await waiting
        assert result['event']['kind'] == 'planner_work'
        assert result['event']['planner_job_id'] == 'plan-event'
    asyncio.run(check())


def test_barrier_rearms_a_wakeup_the_agent_already_acknowledged(service, actors):
    # The delivery key is per queue item. A barrier cancels the offer the
    # developer was told about; without re-arming, the reissued offer after
    # Planner resolves never wakes anyone and the agent idles for hours.
    from hoh_control_plane.event_wait import wait_for_event
    register(service, actors['developer'])
    release(service, actors)
    planner = actors['planner']

    async def scenario():
        first = await wait_for_event(service, actors['developer'], 'project-1', timeout_seconds=1)
        assert first['event']['kind'] == 'work_available'
        with service.store.connection(immediate=True) as connection:
            reconciliation_id = service._open_commit_reconciliation(
                connection, planner, 'project-1', commit='a' * 40,
                subject='teammate push', relation='fast_forward',
                observation_ref='test:push')
        blocked = await wait_for_event(service, actors['developer'], 'project-1',
                                       timeout_seconds=.2, ack_event_id=first['event_id'])
        assert blocked['event']['kind'] != 'work_available', blocked
        pending = service.list_repository_reconciliations(planner, 'project-1')
        decisions = [{'ticket_id': ticket_id, 'disposition': 'keep',
                      'reason': 'unaffected by the push'}
                     for ticket_id in pending['reconciliations'][0]['ticket_versions']]
        service.resolve_repository_reconciliation(
            planner, 'project-1', reconciliation_id, decisions, 'push does not touch DEV-142')
        again = await wait_for_event(service, actors['developer'], 'project-1', timeout_seconds=2,
                                     ack_event_id=blocked['event_id'])
        assert again['status'] == 'event'
        assert again['event']['ticket_ref'] == 'ticket:DEV-142'
    asyncio.run(scenario())


def _abandoned_planner_job(service, actors):
    import dataclasses
    with service.store.connection(immediate=True) as db:
        db.execute("INSERT INTO planner_jobs (planner_job_id,project_id,ticket_id,job_type,status,summary,evidence_refs,created_by,created_at,updated_at) VALUES ('plan-dead','project-1','DEV-142','developer_blocker','READY','Clarify requirement','[]','dev-01','now','now')")
    dead = dataclasses.replace(actors['planner'], agent_id='planner-dead')
    claimed = service.claim_planner_job(dead, 'plan-dead', lease_seconds=30)
    assert claimed['planner_job']['status'] == 'IN_PROGRESS'
    with service.store.connection(immediate=True) as db:
        db.execute("UPDATE planner_job_leases SET expires_at='2000-01-01T00:00:00Z' WHERE planner_job_id='plan-dead'")


def _planner_job_status(service):
    with service.store.connection() as db:
        return db.execute("SELECT status FROM planner_jobs WHERE planner_job_id='plan-dead'").fetchone()['status']


def test_sweep_requeues_an_abandoned_planner_job(service, actors):
    _abandoned_planner_job(service, actors)
    admin = Actor('hoh-scheduler', Role.ADMIN, frozenset({'*'}))
    result = service.sweep_expired_leases(admin)
    assert result['planner_jobs_requeued'] == ['plan-dead']
    assert _planner_job_status(service) == 'READY'


def test_waiting_planner_is_woken_by_an_abandoned_planner_job(service, actors, monkeypatch):
    from hoh_control_plane import event_wait
    from hoh_control_plane.event_wait import wait_for_event
    monkeypatch.setitem(event_wait._REPOSITORY_OBSERVED_AT, 'project-1', time.monotonic())
    register(service, actors['planner'])
    _abandoned_planner_job(service, actors)
    result = asyncio.run(wait_for_event(service, actors['planner'], 'project-1', timeout_seconds=2))
    assert result['event']['kind'] == 'planner_work'
    assert result['event']['planner_job_id'] == 'plan-dead'
    assert _planner_job_status(service) == 'READY'
