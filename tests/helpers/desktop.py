"""The local desktop control app on a throwaway install root."""
import json

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
