"""Desktop child environments on a disposable installation, without a host start."""
import importlib.util
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import stat

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


def test_temp_setup_failure_does_not_publish_environment(launcher, monkeypatch):
    parent = dict(os.environ)
    def denied(*args, **kwargs):
        raise OSError('synthetic-secret-must-not-escape')
    monkeypatch.setattr(Path, 'mkdir', denied)
    with pytest.raises(RuntimeError, match='Cannot establish private installation') as failure:
        launcher.environment('mcp')
    assert 'synthetic-secret' not in str(failure.value)
    assert dict(os.environ) == parent


def test_temp_refuses_existing_file(launcher):
    (launcher.ROOT / 't').write_text('retained', encoding='utf-8')
    with pytest.raises(RuntimeError, match='Cannot establish private installation'):
        launcher.environment('supervisor')
    assert (launcher.ROOT / 't').read_text(encoding='utf-8') == 'retained'


@pytest.mark.skipif(os.name == 'nt', reason='POSIX mode contract')
def test_existing_posix_temp_is_restricted(launcher):
    temp = launcher.ROOT / 't'
    temp.mkdir(mode=0o777)
    temp.chmod(0o777)
    launcher.environment('supervisor')
    assert stat.S_IMODE(temp.stat().st_mode) == 0o700


def windows_script(script, path):
    env = {k: v for k, v in os.environ.items() if k.upper() != 'PSMODULEPATH'}
    env['HOH_TEST_PATH'] = str(path)
    executable = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    return subprocess.run([str(executable), '-NoProfile', '-NonInteractive', '-EncodedCommand',
        base64.b64encode(script.encode('utf-16le')).decode('ascii')], env=env,
        capture_output=True, text=True, check=True, timeout=30,
        creationflags=subprocess.CREATE_NO_WINDOW).stdout


def windows_acl(path):
    return json.loads(windows_script(r'''
$ErrorActionPreference = 'Stop'
$acl = Get-Acl -LiteralPath $env:HOH_TEST_PATH
@{protected=$acl.AreAccessRulesProtected; owner=$acl.GetOwner([Security.Principal.SecurityIdentifier]).Value;
group=$acl.GetGroup([Security.Principal.SecurityIdentifier]).Value;
user=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value;
rules=@($acl.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier]) | ForEach-Object {
@{sid=$_.IdentityReference.Value; rights=[int]$_.FileSystemRights; type=$_.AccessControlType.ToString();
inherited=$_.IsInherited; inheritance=[int]$_.InheritanceFlags; propagation=[int]$_.PropagationFlags}})} | ConvertTo-Json -Depth 5
''', path))


@pytest.mark.skipif(os.name != 'nt', reason='Windows DACL contract')
@pytest.mark.parametrize('existing', [False, True])
def test_windows_broad_installation_yields_private_temp_and_child(launcher, existing):
    # Real broad inheritable parent, matching the independent QA counterexample.
    subprocess.run(['icacls', str(launcher.ROOT), '/grant', '*S-1-5-32-545:(OI)(CI)M'],
                   capture_output=True, check=True, timeout=30)
    temp = launcher.ROOT / 't'
    if existing:
        temp.mkdir()
        before = windows_acl(temp)
        assert any(rule['sid'] == 'S-1-5-32-545' for rule in before['rules'])
    else:
        before = None
    env = launcher.environment('supervisor')
    acl = windows_acl(temp)
    expected = {acl['user'], 'S-1-5-18', 'S-1-5-32-544'}
    assert acl['protected'] and acl['owner'] == acl['user']
    assert len(acl['rules']) == 3
    assert {rule['sid'] for rule in acl['rules']} == expected
    assert all(rule['rights'] == 2032127 and rule['type'] == 'Allow' and
        not rule['inherited'] and rule['inheritance'] == 3 and rule['propagation'] == 0
        for rule in acl['rules'])
    if before:
        assert (acl['owner'], acl['group']) == (before['owner'], before['group'])
    child = subprocess.run([sys.executable, '-I', '-c',
        'import tempfile; f=tempfile.NamedTemporaryFile(delete=False); print(f.name); f.close()'],
        env=env, capture_output=True, text=True, check=True, timeout=30)
    file = Path(child.stdout.strip())
    assert file.parent == temp
    inherited = windows_acl(file)
    assert {rule['sid'] for rule in inherited['rules']} == expected
    assert all(rule['inherited'] and rule['type'] == 'Allow' and rule['rights'] == 2032127
               for rule in inherited['rules'])
    assert launcher.environment('supervisor') == env
    assert windows_acl(temp) == acl


@pytest.mark.skipif(os.name != 'nt', reason='Windows junction contract')
def test_windows_temp_junction_is_refused_without_touching_target(launcher):
    target = launcher.ROOT / 'target'
    target.mkdir()
    sentinel = target / 'keep.txt'
    sentinel.write_text('untouched', encoding='utf-8')
    before = windows_acl(target)
    windows_script("$ErrorActionPreference='Stop'; New-Item -ItemType Junction -Path "
                   "(Join-Path $env:HOH_TEST_PATH 't') -Target "
                   "(Join-Path $env:HOH_TEST_PATH 'target') | Out-Null", launcher.ROOT)
    with pytest.raises(RuntimeError, match='Cannot establish private installation'):
        launcher.environment('supervisor')
    assert sentinel.read_text(encoding='utf-8') == 'untouched'
    assert windows_acl(target) == before


@pytest.mark.skipif(os.name != 'nt', reason='Windows helper failure contract')
def test_windows_acl_failure_is_closed_and_redacted(launcher, monkeypatch):
    parent = dict(os.environ)
    def failed(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 1, 'synthetic-secret', 'synthetic-secret')
    monkeypatch.setattr(launcher.subprocess, 'run', failed)
    with pytest.raises(RuntimeError, match='Cannot establish private installation') as failure:
        launcher.environment('mcp')
    assert 'synthetic-secret' not in str(failure.value)
    assert dict(os.environ) == parent
