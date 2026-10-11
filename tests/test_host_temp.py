"""Desktop child environments on a disposable installation, without a host start."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.fixture
def launcher(tmp_path, monkeypatch):
    source = Path(__file__).resolve().parents[1] / 'deploy/desktop/hoh_desktop.py'
    spec = importlib.util.spec_from_file_location('host_temp_launcher', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    root = tmp_path / 'install'
    (root / 'config').mkdir(parents=True)
    config = root / 'config/settings.json'
    config.write_text(json.dumps({'database_path': str(root / 'unused.db'),
                                 'ui_port': 8765, 'mcp_port': 8931}), encoding='utf-8')
    (root / 'config/credentials.json').write_text('{"fixture-only": {}}', encoding='utf-8')
    monkeypatch.setattr(module, 'ROOT', root)
    monkeypatch.setattr(module, 'CONFIG', config)
    return module


@pytest.mark.parametrize('component', ['host', 'ui', 'mcp', 'scheduler', 'supervisor'])
def test_child_temp_preserves_parent_and_component_environment(launcher, monkeypatch, component):
    monkeypatch.setenv('TEMP', 'original-user-temp')
    monkeypatch.setenv('TMP', 'original-user-tmp')
    monkeypatch.setenv('UNRELATED_SETTING', 'retained')
    for key in ('HOH_MCP_TOKEN_OTHER', 'HOH_MCP_TOKENS_JSON', 'PYTHONPATH',
                'PYTHONHOME', 'COVERAGE_PROCESS_START', 'COV_CORE_SOURCE'):
        monkeypatch.setenv(key, 'discarded')
    parent = dict(os.environ)
    env = launcher.environment(component)
    temp = launcher.ROOT / 't'
    assert temp.is_dir()
    assert env['TEMP'] == env['TMP'] == str(temp)
    assert env['UNRELATED_SETTING'] == 'retained'
    assert env['HOH_DATABASE_PATH'] == str(launcher.ROOT / 'unused.db')
    assert env['HOH_WORKER_MODE'] == 'supervised'
    assert env['HOH_PORT'] == '8765' and env['HOH_MCP_PORT'] == '8931'
    assert all(key not in env for key in ('HOH_MCP_TOKEN_OTHER', 'PYTHONPATH',
        'PYTHONHOME', 'COVERAGE_PROCESS_START', 'COV_CORE_SOURCE'))
    if component == 'mcp':
        assert env['HOH_MCP_TOKENS_JSON'] == '{"fixture-only": {}}'
        assert env['HOH_LOCAL_CREDENTIALS_PATH'] == str(launcher.ROOT / 'config/agent-credentials.json')
    else:
        assert 'HOH_MCP_TOKENS_JSON' not in env
        assert 'HOH_LOCAL_CREDENTIALS_PATH' not in env
    assert dict(os.environ) == parent
    assert launcher.environment(component) == env  # Existing directory is reusable.
    assert not (launcher.ROOT / 'unused.db').exists()


def test_real_python_and_descendant_use_installation_temp(launcher):
    env = launcher.environment('supervisor')
    probe = (
        'import json, pathlib, tempfile; '
        'f=tempfile.NamedTemporaryFile(); '
        'print(json.dumps([tempfile.gettempdir(), str(pathlib.Path(f.name).parent)]))'
    )
    # A second Python represents the worker spawned by the supervisor component.
    child = subprocess.run([sys.executable, '-I', '-c',
        'import subprocess, sys; subprocess.run([sys.executable, "-I", "-c", sys.argv[1]], check=True)',
        probe], env=env, cwd=launcher.ROOT, capture_output=True, text=True, check=True, timeout=30)
    assert [Path(value) for value in json.loads(child.stdout)] == [launcher.ROOT / 't'] * 2
    assert not (launcher.ROOT / 'unused.db').exists()
