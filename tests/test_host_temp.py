"""Desktop child environments on a disposable installation, without a host start."""
import importlib.util
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import stat
import tempfile

import pytest


@pytest.fixture
def launcher_boundary(tmp_path):
    if os.name != 'nt':
        yield tmp_path
        return
    # Keep short Windows paths without leaving one directory per case at D:/.
    parent = Path('D:/.hoh-test-temp')
    parent.mkdir(exist_ok=True)
    if parent.is_symlink() or parent.is_junction():
        raise RuntimeError('Test temp root must not be a reparse point')
    protect_windows_directory(parent)
    with tempfile.TemporaryDirectory(prefix='host-', dir=parent) as directory:
        boundary = Path(directory)
        protect_windows_directory(boundary)
        yield boundary


@pytest.fixture
def launcher(launcher_boundary, monkeypatch):
    source = Path(__file__).resolve().parents[1] / 'deploy/desktop/hoh_desktop.py'
    spec = importlib.util.spec_from_file_location('host_temp_launcher', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    boundary = launcher_boundary
    root = boundary / 'install'
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


@pytest.mark.skipif(os.name != 'nt', reason='Windows fixture cleanup contract')
@pytest.mark.parametrize('failed', [False, True])
def test_windows_fixture_cleans_its_boundary_without_following_junction(tmp_path, failed):
    target = tmp_path / 'retained'
    target.mkdir()
    sentinel = target / 'keep.txt'
    sentinel.write_text('untouched', encoding='utf-8')
    fixture = launcher_boundary.__wrapped__(tmp_path)
    boundary = next(fixture)
    try:
        assert boundary.parent == Path('D:/.hoh-test-temp')
        (boundary / 'owned.txt').write_text('test data', encoding='utf-8')
        # Cleanup must unlink the junction, preserving the unrelated target.
        windows_script("$ErrorActionPreference='Stop'; New-Item -ItemType Junction -Path "
                       "(Join-Path $env:HOH_TEST_PATH 'alias') -Target '" +
                       str(target).replace("'", "''") + "' | Out-Null", boundary)
        if failed:
            with pytest.raises(RuntimeError, match='synthetic case failure'):
                fixture.throw(RuntimeError('synthetic case failure'))
        else:
            fixture.close()
        assert not boundary.exists()
        assert sentinel.read_text(encoding='utf-8') == 'untouched'
    finally:
        fixture.close()


@pytest.mark.skipif(os.name != 'nt', reason='Windows DACL contract')
@pytest.mark.parametrize('existing', [False, True])
def test_windows_broad_installation_yields_private_temp_and_child(launcher, existing):
    # Broad inheritable read access must not reach confidential temp files.
    subprocess.run(['icacls', str(launcher.ROOT), '/grant', '*S-1-5-32-545:(OI)(CI)RX'],
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


def protect_windows_directory(path):
    windows_script(r"""
$ErrorActionPreference = 'Stop'
$d = [IO.DirectoryInfo]::new($env:HOH_TEST_PATH)
$acl = $d.GetAccessControl([Security.AccessControl.AccessControlSections]::Access)
$acl.SetAccessRuleProtection($true,$false)
foreach ($r in @($acl.GetAccessRules($true,$false,[Security.Principal.SecurityIdentifier]))) { $acl.RemoveAccessRuleAll($r) }
foreach ($sid in @([Security.Principal.WindowsIdentity]::GetCurrent().User.Value, 'S-1-5-18', 'S-1-5-32-544')) {
$acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new([Security.Principal.SecurityIdentifier]::new($sid),'FullControl','ContainerInherit,ObjectInherit','None','Allow'))
}
$d.SetAccessControl($acl)
""", path)


def configure_temp(launcher, path):
    config = launcher.settings()
    config['private_temp_path'] = str(path)
    launcher.CONFIG.write_text(json.dumps(config), encoding='utf-8')


def external_temp(launcher):
    temp = launcher.ROOT.parent / 'external'
    temp.mkdir(mode=0o700)
    if os.name == 'nt':
        protect_windows_directory(temp)
    return temp


def test_explicit_private_temp_and_real_child(launcher):
    temp = external_temp(launcher)
    configure_temp(launcher, temp)
    before = windows_acl(temp) if os.name == 'nt' else temp.stat()
    parent = dict(os.environ)
    env = launcher.environment('supervisor')
    assert env['TEMP'] == env['TMP'] == str(temp)
    child = subprocess.run([sys.executable, '-I', '-c',
        'import tempfile; f=tempfile.NamedTemporaryFile(); print(tempfile.gettempdir())'],
        env=env, capture_output=True, text=True, check=True, timeout=30)
    assert Path(child.stdout.strip()) == temp
    assert (windows_acl(temp) if os.name == 'nt' else temp.stat().st_mode) == (before if os.name == 'nt' else before.st_mode)
    assert dict(os.environ) == parent
    assert not (launcher.ROOT / 't').exists()


@pytest.mark.parametrize('value', ['', 'relative/private', None, 7, r'\\server\share\private', 'D:relative', 'D:/private/../private'])
def test_explicit_invalid_path_is_redacted_without_fallback(launcher, value):
    config = launcher.settings()
    config['private_temp_path'] = value
    launcher.CONFIG.write_text(json.dumps(config), encoding='utf-8')
    parent = dict(os.environ)
    with pytest.raises(RuntimeError, match='Cannot establish private installation'):
        launcher.environment('supervisor')
    assert not (launcher.ROOT / 't').exists()
    assert dict(os.environ) == parent


def test_explicit_missing_path_is_not_created(launcher):
    temp = launcher.ROOT.parent / 'missing-sensitive-name'
    configure_temp(launcher, temp)
    with pytest.raises(RuntimeError, match='Cannot establish private installation') as failure:
        launcher.environment('supervisor')
    assert 'sensitive-name' not in str(failure.value)
    assert not temp.exists() and not (launcher.ROOT / 't').exists()


@pytest.mark.parametrize('location', ['install', 'data', 'case-data'])
def test_explicit_protected_location_is_refused(launcher, location):
    temp = launcher.ROOT / 'external' if location == 'install' else Path('D:/HOH/DATA' if location == 'case-data' else 'D:/HoH/data')
    if location == 'install':
        temp.mkdir()
    configure_temp(launcher, temp)
    with pytest.raises(RuntimeError, match='Cannot establish private installation'):
        launcher.environment('supervisor')
    assert not (launcher.ROOT / 't').exists()


@pytest.mark.skipif(os.name != 'nt', reason='Windows DACL contract')
@pytest.mark.parametrize('grant', ['*S-1-5-32-545:(OI)(CI)R', '*S-1-5-11:(OI)(CI)M'])
def test_explicit_broad_acl_is_refused_without_repair(launcher, grant):
    temp = external_temp(launcher)
    subprocess.run(['icacls', str(temp), '/grant', grant], capture_output=True, check=True, timeout=30)
    before = windows_acl(temp)
    configure_temp(launcher, temp)
    with pytest.raises(RuntimeError, match='Cannot establish private installation'):
        launcher.environment('supervisor')
    assert windows_acl(temp) == before
    assert not (launcher.ROOT / 't').exists()


@pytest.mark.skipif(os.name != 'nt', reason='Windows ancestor replacement contract')
@pytest.mark.parametrize('grant', ['*S-1-5-32-545:(DC)', '*S-1-5-32-545:(DE)', '*S-1-5-32-545:(WDAC)', '*S-1-5-32-545:(WO)'])
def test_explicit_parent_replacement_authority_is_refused(launcher, grant):
    temp = external_temp(launcher)
    subprocess.run(['icacls', str(temp.parent), '/grant', grant], capture_output=True, check=True, timeout=30)
    before = windows_acl(temp)
    configure_temp(launcher, temp)
    with pytest.raises(RuntimeError, match='Cannot establish private installation'):
        launcher.environment('supervisor')
    assert windows_acl(temp) == before


@pytest.mark.skipif(os.name != 'nt', reason='Windows ancestor junction contract')
def test_explicit_ancestor_junction_is_refused(launcher):
    temp = external_temp(launcher)
    before = windows_acl(temp)
    windows_script("$ErrorActionPreference='Stop'; New-Item -ItemType Junction -Path "
        "(Join-Path $env:HOH_TEST_PATH 'alias') -Target $env:HOH_TEST_PATH | Out-Null", temp.parent)
    configure_temp(launcher, temp.parent / 'alias' / temp.name)
    with pytest.raises(RuntimeError, match='Cannot establish private installation'):
        launcher.environment('supervisor')
    assert windows_acl(temp) == before


@pytest.mark.skipif(os.name != 'nt', reason='Windows owner verification contract')
def test_explicit_wrong_owner_is_refused_without_repair(launcher):
    temp = external_temp(launcher)
    # Administrators is a legal owner available to this operator's Windows token.
    result = subprocess.run(['icacls', str(temp), '/setowner', '*S-1-5-32-544'], capture_output=True, timeout=30)
    if result.returncode:
        pytest.skip('Windows token cannot assign an alternate owner')
    before = windows_acl(temp)
    assert before['owner'] != before['user']
    configure_temp(launcher, temp)
    with pytest.raises(RuntimeError, match='Cannot establish private installation'):
        launcher.environment('supervisor')
    assert windows_acl(temp) == before


@pytest.mark.skipif(os.name == 'nt', reason='POSIX verification-only contract')
def test_explicit_posix_permissions_are_not_repaired(launcher):
    temp = external_temp(launcher)
    temp.chmod(0o755)
    configure_temp(launcher, temp)
    with pytest.raises(RuntimeError, match='Cannot establish private installation'):
        launcher.environment('supervisor')
    assert stat.S_IMODE(temp.stat().st_mode) == 0o755


@pytest.mark.skipif(os.name != 'nt', reason='Windows protected inheritance contract')
def test_explicit_inherited_private_acl_is_refused_without_repair(launcher):
    temp = launcher.ROOT.parent / 'inherited'
    temp.mkdir()
    before = windows_acl(temp)
    assert not before['protected'] and all(rule['inherited'] for rule in before['rules'])
    configure_temp(launcher, temp)
    with pytest.raises(RuntimeError, match='Cannot establish private installation'):
        launcher.environment('supervisor')
    assert windows_acl(temp) == before


@pytest.mark.skipif(os.name != 'nt', reason='Windows explicit leaf junction contract')
def test_explicit_leaf_junction_is_refused_without_touching_target(launcher):
    temp = external_temp(launcher)
    before = windows_acl(temp)
    windows_script("$ErrorActionPreference='Stop'; New-Item -ItemType Junction -Path "
        "(Join-Path $env:HOH_TEST_PATH 'alias') -Target "
        "(Join-Path $env:HOH_TEST_PATH 'external') | Out-Null", temp.parent)
    configure_temp(launcher, temp.parent / 'alias')
    with pytest.raises(RuntimeError, match='Cannot establish private installation'):
        launcher.environment('supervisor')
    assert windows_acl(temp) == before


@pytest.mark.skipif(os.name != 'nt', reason='Windows default ancestor replacement contract')
def test_default_refuses_replaceable_installation_before_publication(launcher):
    subprocess.run(['icacls', str(launcher.ROOT), '/grant', '*S-1-5-32-545:(DE)'],
                   capture_output=True, check=True, timeout=30)
    before = windows_acl(launcher.ROOT)
    parent = dict(os.environ)
    with pytest.raises(RuntimeError, match='Cannot establish private installation'):
        launcher.environment('supervisor')
    assert windows_acl(launcher.ROOT) == before
    assert dict(os.environ) == parent
    assert not (launcher.ROOT / 'unused.db').exists()


@pytest.mark.skipif(os.name != 'nt', reason='Windows null parent DACL contract')
def test_explicit_null_parent_dacl_is_refused(launcher):
    parent = launcher.ROOT.parent / 'null-parent'
    parent.mkdir()
    protect_windows_directory(parent)
    temp = parent / 'leaf'
    temp.mkdir()
    protect_windows_directory(temp)
    before = windows_acl(temp)
    windows_script(r"""
$ErrorActionPreference = 'Stop'
$d = [IO.DirectoryInfo]::new($env:HOH_TEST_PATH)
$a = $d.GetAccessControl([Security.AccessControl.AccessControlSections]::Access)
$a.SetSecurityDescriptorSddlForm('D:NO_ACCESS_CONTROL', [Security.AccessControl.AccessControlSections]::Access)
$d.SetAccessControl($a)
$raw = [Security.AccessControl.RawSecurityDescriptor]::new((Get-Acl -LiteralPath $env:HOH_TEST_PATH).GetSecurityDescriptorBinaryForm(), 0)
if ($null -ne $raw.DiscretionaryAcl) { throw 'Fixture did not produce null DACL' }
""", temp.parent)
    configure_temp(launcher, temp)
    with pytest.raises(RuntimeError, match='Cannot establish private installation'):
        launcher.environment('supervisor')
    assert windows_acl(temp) == before


@pytest.mark.skipif(os.name != 'nt', reason='Windows drive type boundary')
def test_explicit_remote_drive_type_is_refused_without_mutation(launcher, monkeypatch):
    temp = external_temp(launcher)
    before = windows_acl(temp)
    configure_temp(launcher, temp)
    # Inject only the OS drive-type result; do not mount a network drive.
    monkeypatch.setattr(launcher.ctypes.windll.kernel32, 'GetDriveTypeW', lambda *args: 4)
    with pytest.raises(RuntimeError, match='Cannot establish private installation'):
        launcher.environment('supervisor')
    assert windows_acl(temp) == before
    assert not (launcher.ROOT / 't').exists()


@pytest.mark.skipif(os.name != 'nt', reason='Windows final physical identity boundary')
@pytest.mark.parametrize('target', ['installation', 'data'])
def test_explicit_alias_into_protected_physical_location_is_refused(launcher, monkeypatch, target):
    temp = external_temp(launcher)
    before = windows_acl(temp)
    configure_temp(launcher, temp)
    realpath = launcher.os.path.realpath
    forbidden = launcher.ROOT if target == 'installation' else Path('D:/HoH/data')
    physical = realpath(forbidden, strict=True)
    # Simulate the native final-path result of a drive alias, without changing
    # the machine's drive mappings or touching the protected target.
    def alias(path, **kwargs):
        return physical if Path(path) == temp else realpath(path, **kwargs)
    monkeypatch.setattr(launcher.os.path, 'realpath', alias)
    with pytest.raises(RuntimeError, match='Cannot establish private installation'):
        launcher.environment('supervisor')
    assert windows_acl(temp) == before
    assert not (launcher.ROOT / 't').exists()
