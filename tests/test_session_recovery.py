import asyncio
import json
import subprocess
import sys

import pytest

from hoh_control_plane.domain import AuthorizationError, ConflictError


@pytest.fixture(autouse=True)
def no_ready_work(service, seeded):
    with service.store.connection(immediate=True) as db:
        db.execute("UPDATE tickets SET status='BACKLOG' WHERE ticket_id='DEV-142'")
        db.execute("UPDATE work_queue SET status='cancelled' WHERE ticket_id='DEV-142'")


@pytest.mark.parametrize('role', ['planner', 'developer', 'qa', 'integrator'])
def test_reconnect_resumes_own_wait_and_fences_previous_call(service, actors, role):
    from hoh_control_plane.event_wait import wait_for_event
    actor = actors[role]
    service.register_agent(actor, skills=[role], capacity=1)

    async def scenario():
        previous = asyncio.create_task(wait_for_event(service, actor, 'project-1', timeout_seconds=5))
        replacement = None
        try:
            await asyncio.sleep(.04)
            bootstrap = service.session_bootstrap(actor, 'project-1')
            resume_id = bootstrap['event_wait']['wait_id']
            assert bootstrap['event_wait']['state'] == 'waiting'
            assert service.session_bootstrap(actors['developer_2'], 'project-1')['event_wait'] is None
            replacement = asyncio.create_task(wait_for_event(
                service, actor, 'project-1', timeout_seconds=5, resume_wait_id=resume_id))
            old_result = await asyncio.wait_for(previous, 1)
            assert old_result['status'] == 'superseded'
            assert old_result['event'] is None
            current = service.session_bootstrap(actor, 'project-1')['event_wait']
            assert current['wait_id'] != resume_id
            assert current['state'] == 'waiting'
            assert not replacement.done()
            # A delayed retry for the old generation must not evict the new one.
            with pytest.raises(ConflictError):
                await wait_for_event(service, actor, 'project-1', timeout_seconds=.05, resume_wait_id=resume_id)
            with service.store.connection() as db:
                assert db.execute('SELECT count(*) FROM leases').fetchone()[0] == 0
                assert db.execute('SELECT expires_at FROM agent_presence WHERE agent_id=?', (actor.agent_id,)).fetchone()[0] > current['updated_at']
        finally:
            for task in (previous, replacement):
                if task and not task.done():
                    task.cancel()
            await asyncio.gather(*[task for task in (previous, replacement) if task], return_exceptions=True)
    asyncio.run(scenario())


def test_expired_wait_cannot_resume_delivery_after_a_new_wait_takes_over(service, actors, monkeypatch):
    from hoh_control_plane.event_wait import wait_for_event
    actor = actors['developer']
    service.register_agent(actor, skills=['developer'], capacity=1)

    async def scenario():
        previous = asyncio.create_task(wait_for_event(service, actor, 'project-1', timeout_seconds=2))
        replacement = None
        try:
            await asyncio.sleep(.04)
            with service.store.connection(immediate=True) as db:
                db.execute("UPDATE agent_event_waiters SET expires_at='2000-01-01' WHERE agent_id=?", (actor.agent_id,))
            replacement = asyncio.create_task(wait_for_event(service, actor, 'project-1', timeout_seconds=2))
            await asyncio.sleep(.04)
            service.set_ticket_ready(actors['planner'], 'DEV-142', expected_version=1)
            old, new = await asyncio.wait_for(asyncio.gather(previous, replacement), 1)
            assert old['status'] == 'superseded'
            assert new['status'] == 'event'
            assert service.session_bootstrap(actor, 'project-1')['event_wait']['state'] == 'notified'
        finally:
            for task in (previous, replacement):
                if task and not task.done():
                    task.cancel()
            await asyncio.gather(*[task for task in (previous, replacement) if task], return_exceptions=True)
    asyncio.run(scenario())


def test_failed_transport_keepalive_releases_wait_presence(service, actors):
    from hoh_control_plane.event_wait import wait_for_event, session_status
    actor = actors['qa']
    service.register_agent(actor, skills=['qa'], capacity=1)
    async def gone():
        raise ConnectionError('The MCP client has disconnected')
    async def scenario():
        with pytest.raises(ConnectionError):
            await wait_for_event(service, actor, 'project-1', timeout_seconds=10, keepalive=gone)
        assert session_status(service, 'project-1')[0]['state'] == 'disconnected'
        # A retry may start immediately instead of waiting for a 90-second expiry.
        assert (await wait_for_event(service, actor, 'project-1', timeout_seconds=.05))['status'] == 'timeout'
    asyncio.run(scenario())


def test_git_child_cannot_consume_mcp_protocol_input(tmp_path):
    # Model a Git helper reading stdin while the MCP transport owns that pipe.
    # Run outside pytest's substituted stdin to exercise the real OS descriptor.
    script = '''
import subprocess, sys
from pathlib import Path
from unittest.mock import patch
from hoh_control_plane.git_worktrees import GitWorktreeManager
real_run = subprocess.run
def child(command, **kwargs):
    return real_run([sys.executable, '-c', 'import sys; print(repr(sys.stdin.read()))'], **kwargs)
with patch('hoh_control_plane.git_worktrees.subprocess.run', side_effect=child):
    result = GitWorktreeManager._git(Path.cwd(), 'rev-parse', '--show-toplevel')
    print(result.stdout.strip())
'''
    result = subprocess.run([sys.executable, '-c', script], input='{"jsonrpc":"2.0","id":7}\n',
                            text=True, capture_output=True, timeout=8, check=True)
    assert result.stdout.strip() == "''", 'Git inherited and consumed MCP protocol input'
