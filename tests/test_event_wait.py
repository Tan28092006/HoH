import asyncio
import json
import socket
import time

import pytest

from hoh_control_plane.domain import Actor, Role, AuthorizationError, ConflictError


def register(service, actor):
    service.register_agent(actor, skills=[actor.role.value], capacity=1)


def test_wait_delivers_committed_work_without_model_polling(service, actors, seeded):
    from hoh_control_plane.event_wait import wait_for_event
    register(service, actors['developer'])
    async def scenario():
        waiting = asyncio.create_task(wait_for_event(service, actors['developer'], 'project-1', timeout_seconds=4))
        await asyncio.sleep(.1)
        assert not waiting.done()
        started = time.monotonic()
        service.release_ticket(actors['planner'], 'DEV-142')
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
    service.release_ticket(actors['planner'], 'DEV-142')
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
    service.release_ticket(actors['planner'], 'DEV-142')
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
    service.release_ticket(actors['planner'], 'DEV-142')
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
