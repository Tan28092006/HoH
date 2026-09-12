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
