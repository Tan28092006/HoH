import json
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
