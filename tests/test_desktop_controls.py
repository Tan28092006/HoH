import json
from contextlib import closing
from pathlib import Path
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def desktop(tmp_path, monkeypatch):
    from hoh_control_plane.desktop_control import create_desktop_app
    for key in ('HOH_DATABASE_URL', 'HOH_DATABASE_PATH', 'HOH_ARTIFACT_ROOT'):
        monkeypatch.delenv(key, raising=False)
    root = tmp_path / 'desktop'
    (root / 'config').mkdir(parents=True)
    (root / 'config/settings.json').write_text(json.dumps({'database_path': str(root / 'data/control.db'),
        'ui_port': 8765, 'mcp_port': 8931, 'project_id': 'alpha', 'installation_id': 'test-install'}))
    app = create_desktop_app(root)
    client = TestClient(app, base_url='http://127.0.0.1:8765')
    return client, root


def headers(client):
    session = client.get('/local/session').json()
    return {'Origin': 'http://127.0.0.1:8765', 'X-HoH-Local-Token': session['csrf_token']}


def test_create_project_lists_and_selects_exact_id(desktop):
    client, root = desktop
    response = client.post('/local/projects', json={'project_id': 'shop', 'name': 'My shop', 'default_branch': 'main'}, headers=headers(client))
    assert response.status_code == 201
    assert response.json()['project_id'] == 'shop'
    session = client.get('/local/session').json()
    assert [p['project_id'] for p in session['projects']] == ['shop']
    assert client.post('/local/projects', json={'project_id': 'shop', 'name': 'Duplicate'}, headers=headers(client)).status_code == 409
    assert client.post('/local/projects', json={'project_id': '../escape', 'name': 'Bad'}, headers=headers(client)).status_code == 422
    prompts = client.get('/local/prompts?project_id=shop').json()['prompts']
    assert set(prompts) == {'planner', 'developer', 'qa', 'integrator'}
    assert all('shop' in prompt and 'session.bootstrap' in prompt for prompt in prompts.values())
    assert client.get('/local/prompts?project_id=missing').status_code == 404


@pytest.mark.parametrize('bad_headers', [{}, {'Origin': 'https://evil.example'},
    {'Origin': 'http://127.0.0.1:8765', 'X-HoH-Local-Token': 'wrong'}])
def test_external_or_unproved_writes_are_refused(desktop, bad_headers):
    client, root = desktop
    assert client.post('/local/shutdown', headers=bad_headers).status_code == 403
    assert not (root / 'config/shutdown.request').exists()
    assert client.post('/api/v1/projects', json={}).status_code == 405
    assert client.get('/local/session', headers={'Host': 'evil.example'}).status_code == 403


def test_shutdown_pauses_admission_and_requests_drain(desktop):
    client, root = desktop
    response = client.post('/local/shutdown', headers=headers(client))
    assert response.status_code == 202
    assert json.loads((root / 'config/shutdown.request').read_text())['resume_on_start'] is True
    assert client.get('/local/session').json()['shutting_down'] is True
    assert client.post('/local/projects', json={'project_id': 'late', 'name': 'Late'}, headers=headers(client)).status_code == 409
    assert client.post('/local/shutdown', headers=headers(client)).status_code == 202
    assert json.loads((root / 'config/shutdown.request').read_text())['resume_on_start'] is True


def test_project_bound_agent_installs_both_clients_and_preserves_other_settings(desktop, tmp_path):
    from hoh_control_plane.local_agent import install_profile, profile_actor
    client, root = desktop
    client.post('/local/projects', json={'project_id': 'shop', 'name': 'Shop'}, headers=headers(client))
    for client_name in ('codex', 'claude-code'):
        home = tmp_path / client_name
        (home / '.codex').mkdir(parents=True)
        (home / '.codex/config.toml').write_text('model = "existing-model"\n')
        (home / '.claude.json').write_text('{"unrelated":42}')
        for role in ('planner', 'developer', 'qa', 'integrator'):
            result = install_profile(root, 'shop', role, 'primary', client_name, home)
            repeated = install_profile(root, 'shop', role, 'primary', client_name, home)
            assert result['server_name'] == repeated['server_name']
            actor = profile_actor('shop', role, 'primary')
            assert actor.project_ids == frozenset({'shop'}) and actor.role.value == role
        assert 'existing-model' in (home / '.codex/config.toml').read_text()
        assert json.loads((home / '.claude.json').read_text())['unrelated'] == 42
    assert profile_actor('shop', 'developer', 'second').agent_id != profile_actor('shop', 'developer', 'primary').agent_id
    with pytest.raises(Exception):
        install_profile(root, 'missing', 'planner', 'primary', 'codex', tmp_path / 'other')


def test_drain_waits_for_workers_and_reopen_restores_admission(desktop):
    import sqlite3
    from hoh_control_plane.desktop_control import shutdown_ready, prepare_desktop_start
    client, root = desktop
    client.post('/local/shutdown', headers=headers(client))
    assert not shutdown_ready(root)  # A quiet period allows an in-flight tick to publish.
    marker = root / 'config/shutdown.request'
    content = json.loads(marker.read_text())
    content['requested_at'] = 0
    marker.write_text(json.dumps(content))
    database = json.loads((root / 'config/settings.json').read_text())['database_path']
    with closing(sqlite3.connect(database)) as db, db:
        db.execute("INSERT INTO worker_executions (execution_id, runner_id, job_kind, job_id, status, started_at, heartbeat_at) VALUES ('x','test','qa','test-job','RUNNING','now','now')")
    assert not shutdown_ready(root)
    with closing(sqlite3.connect(database)) as db, db:
        db.execute("UPDATE worker_executions SET status='FINISHED' WHERE execution_id='x'")
    assert shutdown_ready(root)
    prepare_desktop_start(root)
    assert not marker.exists()
    with closing(sqlite3.connect(database)) as db:
        assert db.execute("SELECT state_value FROM runtime_state WHERE state_key='paused'").fetchone()[0] == '0'


def test_explicit_skill_upgrade_keeps_backup_and_rolls_back_config_conflict(desktop, tmp_path):
    from hoh_control_plane.local_agent import install_profile
    client, root = desktop
    client.post('/local/projects', json={'project_id': 'shop', 'name': 'Shop'}, headers=headers(client))
    home = tmp_path / 'upgrade-client'
    result = install_profile(root, 'shop', 'planner', 'primary', 'codex', home)
    skill = home / '.codex/skills/hoh-planner/SKILL.md'
    skill.write_text('old user customization')
    with pytest.raises(ValueError):
        install_profile(root, 'shop', 'planner', 'primary', 'codex', home)
    upgraded = install_profile(root, 'shop', 'planner', 'primary', 'codex', home, upgrade_skills=True)
    assert len(upgraded['skill_backups']) == 1
    assert (Path(upgraded['skill_backups'][0]) / 'SKILL.md').read_text() == 'old user customization'
    config = home / '.codex/config.toml'
    config.write_text(config.read_text().replace('hoh_control_plane.local_agent', 'conflicting_module'))
    skill.write_text('preserve me on failure')
    with pytest.raises(ValueError):
        install_profile(root, 'shop', 'planner', 'primary', 'codex', home, upgrade_skills=True)
    assert skill.read_text() == 'preserve me on failure'


def test_stdio_profile_authenticates_exact_role_and_rejects_other_project(desktop):
    import asyncio
    import os
    import sys
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    client, root = desktop
    client.post('/local/projects', json={'project_id': 'shop', 'name': 'Shop'}, headers=headers(client))
    client.post('/local/projects', json={'project_id': 'other', 'name': 'Other'}, headers=headers(client))
    (root / 'hoh_desktop.py').write_text('pass\n')
    async def check():
        parameters = StdioServerParameters(command=sys.executable,
            args=['-m', 'hoh_control_plane.local_agent', 'serve', '--root', str(root), '--project', 'shop', '--role', 'planner'],
            env={**os.environ, 'HOH_AGENT_ROLE': 'admin', 'HOH_PROJECT_IDS': '*'})
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool('session.bootstrap', {'project_id': 'shop'})
                assert not result.isError
                assert result.structuredContent['identity']['role'] == 'planner'
                assert result.structuredContent['identity']['project_ids'] == ['shop']
                assert result.structuredContent['selected_project_id'] == 'shop'
                denied = await session.call_tool('session.bootstrap', {'project_id': 'other'})
                assert denied.isError
    asyncio.run(check())


def test_agent_reconnect_does_not_undo_operator_shutdown(desktop):
    import os
    import subprocess
    import sys
    client, root = desktop
    client.post('/local/projects', json={'project_id': 'shop', 'name': 'Shop'}, headers=headers(client))
    marker = root / 'unexpected-start'
    (root / 'hoh_desktop.py').write_text(f'from pathlib import Path\nPath({str(marker)!r}).touch()\n')
    (root / 'config/stop.request').touch()
    result = subprocess.run([sys.executable, '-m', 'hoh_control_plane.local_agent', 'serve', '--root', str(root),
        '--project', 'shop', '--role', 'planner'], env=os.environ.copy(), input='', capture_output=True, text=True, timeout=20)
    assert not marker.exists(), 'A reconnect must not silently restart an operator-stopped host'
    assert result.returncode != 0


def test_local_reads_close_database_handles(desktop, monkeypatch):
    import sqlite3
    from hoh_control_plane.local_agent import checked_project
    from hoh_control_plane.desktop_control import shutdown_ready
    client, root = desktop
    client.post('/local/projects', json={'project_id': 'shop', 'name': 'Shop'}, headers=headers(client))
    (root / 'config/shutdown.request').write_text('{"requested_at":0,"resume_on_start":true}')
    connections = []
    connect = sqlite3.connect
    def observed_connect(*args, **kwargs):
        connection = connect(*args, **kwargs)
        connections.append(connection)
        return connection
    monkeypatch.setattr(sqlite3, 'connect', observed_connect)
    checked_project(root, 'shop')
    assert shutdown_ready(root)
    try:
        for connection in connections:
            with pytest.raises(sqlite3.ProgrammingError):
                connection.execute('SELECT 1')
    finally:
        for connection in connections:
            connection.close()


def test_gui_wait_profiles_have_long_timeout_and_unique_instances(desktop, tmp_path):
    import tomllib
    from hoh_control_plane.local_agent import install_profile, make_prompts
    client, root = desktop
    client.post('/local/projects', json={'project_id': 'shop', 'name': 'Shop'}, headers=headers(client))
    home = tmp_path / 'gui-client'
    result = install_profile(root, 'shop', 'developer', 'codex-1', 'codex', home)
    config = home / '.codex/config.toml'
    assert tomllib.loads(config.read_text())['mcp_servers'][result['server_name']]['tool_timeout_sec'] == 86400
    # Upgrading an otherwise identical 0.3.1 profile preserves unrelated config.
    config.write_text('model = "configured-model"\n' + config.read_text().replace('tool_timeout_sec = 86400\n', ''))
    install_profile(root, 'shop', 'developer', 'codex-1', 'codex', home)
    assert tomllib.loads(config.read_text())['model'] == 'configured-model'
    assert tomllib.loads(config.read_text())['mcp_servers'][result['server_name']]['tool_timeout_sec'] == 86400
    prompt = make_prompts(root, 'shop', instance='codex-1')['developer']
    assert 'work.wait_for_event' in prompt
    assert '--instance codex-1' in prompt
    assert result['server_name'] in prompt
    assert client.get('/local/agents?project_id=shop').json() == {'agents': []}


def test_claude_profile_upgrade_handles_idle_timeout_and_preserves_higher_override(desktop, tmp_path):
    from hoh_control_plane.local_agent import install_profile
    client, root = desktop
    client.post('/local/projects', json={'project_id':'shop','name':'Shop'}, headers=headers(client))
    home = tmp_path / 'claude-home'
    result = install_profile(root, 'shop', 'qa', 'primary', 'claude-code', home)
    path = home / '.claude.json'
    data = json.loads(path.read_text())
    name = result['server_name']
    assert data['mcpServers'][name]['timeout'] == 86400000
    data['mcpServers'][name].pop('timeout')
    data['unrelated'] = {'keep':True}
    path.write_text(json.dumps(data))
    install_profile(root, 'shop', 'qa', 'primary', 'claude-code', home)
    upgraded = json.loads(path.read_text())
    assert upgraded['mcpServers'][name]['timeout'] == 86400000
    upgraded['mcpServers'][name]['timeout'] = 90000000
    path.write_text(json.dumps(upgraded))
    install_profile(root, 'shop', 'qa', 'primary', 'claude-code', home)
    preserved = json.loads(path.read_text())
    assert preserved['mcpServers'][name]['timeout'] == 90000000
    assert preserved['unrelated'] == {'keep':True}
