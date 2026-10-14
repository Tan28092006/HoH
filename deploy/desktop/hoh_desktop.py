"""Desktop launcher for the isolated local HoH installation (stdlib + HoH)."""
from __future__ import annotations

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
import ctypes
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import secrets
import socket
import stat
import subprocess
import sys
import threading
import time
from urllib.request import urlopen
import webbrowser

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / 'config/settings.json'
HIDDEN = 0x08000000 if os.name == 'nt' else 0
COMPONENTS = ('ui', 'mcp', 'scheduler', 'supervisor')


def settings():
    return json.loads(CONFIG.read_text(encoding='utf-8'))


def initialize():
    if CONFIG.exists():
        raise RuntimeError('An installation already exists; preserve its configuration')
    for name in ('data', 'logs', 'config', 'kits'):
        (ROOT / name).mkdir(exist_ok=True)
    config = {'installation_id': secrets.token_hex(16), 'ui_port': 8765,
              'mcp_port': 8931, 'project_id': 'hoh-control-plane', 'max_workers': 2,
              'database_path': r'D:\HoH\data\control-plane.db'}
    credentials = {secrets.token_urlsafe(48): {'agent_id': 'local-' + role,
                   'role': role, 'project_ids': ['demo', 'hoh-control-plane']}
                   for role in ('planner', 'developer', 'qa', 'integrator')}
    (ROOT / 'config/credentials.json').write_text(json.dumps(credentials, indent=2), encoding='utf-8')
    CONFIG.write_text(json.dumps(config, indent=2), encoding='utf-8')
    print('Initialized local settings and four private role credentials; values were not printed.')


def private_temp(config=None):
    """Publish a short temp path only after establishing its private boundary."""
    config = settings() if config is None else config
    explicit = 'private_temp_path' in config
    temp = ROOT / 't'
    physical = temp
    try:
        if explicit:
            value = config['private_temp_path']
            if not isinstance(value, str) or not value:
                raise OSError('Invalid path')
            temp = Path(value)
            if (not temp.is_absolute() or '..' in temp.parts or
                    (os.name == 'nt' and len(temp.drive) != 2)):
                raise OSError('Invalid path')
            for forbidden in (ROOT, Path('D:/HoH-service'), Path('D:/HoH/data')):
                if temp.is_relative_to(forbidden):
                    raise OSError('Protected location')
            if not temp.is_dir():
                raise OSError('Missing directory')
            if os.name == 'nt':
                drive_type = ctypes.windll.kernel32.GetDriveTypeW
                drive_type.argtypes = [ctypes.c_wchar_p]
                drive_type.restype = ctypes.c_uint
                if drive_type(temp.anchor) not in (2, 3, 6):  # local removable/fixed/RAM
                    raise OSError('Nonlocal drive')
                physical = Path(os.path.realpath(temp, strict=True))
                if len(physical.drive) != 2:
                    raise OSError('Nonlocal physical path')
            else:
                physical = temp
            # Native file identity also closes aliases with another drive letter.
            for forbidden in (ROOT, Path('D:/HoH-service'), Path('D:/HoH/data')):
                if forbidden.exists():
                    canonical = Path(os.path.realpath(forbidden, strict=True))
                    if (physical.is_relative_to(canonical) or
                            any(os.path.samefile(path, forbidden)
                                for path in (physical, *physical.parents))):
                        raise OSError('Protected physical location')
        for path in (temp, *temp.parents, physical, *physical.parents):
            if path.exists() or path.is_symlink():
                info = path.lstat()
                if (stat.S_ISLNK(info.st_mode) or
                        getattr(info, 'st_file_attributes', 0) & 0x400):
                    raise OSError('Reparse point')
        if not explicit:
            temp.mkdir(mode=0o700, exist_ok=True)
        if os.name != 'nt':
            if temp.stat().st_uid != os.getuid():
                raise OSError('Unexpected temp owner')
            for ancestor in temp.parents:
                info = ancestor.stat()
                if (info.st_uid not in (os.getuid(), 0) or
                        (info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX)):
                    raise OSError('Unsafe ancestor')
            if not explicit:
                temp.chmod(0o700)
            if stat.S_IMODE(temp.stat().st_mode) != 0o700:
                raise OSError('Private mode verification failed')
        else:
            # Request Access only: setting even an unchanged owner requires
            # WRITE_OWNER, which a non-elevated installation owner may lack.
            script = r'''
$ErrorActionPreference = 'Stop'
$temp = $env:HOH_PRIVATE_TEMP
$directory = [IO.DirectoryInfo]::new($temp)
if ($directory.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Reparse point' }
$before = Get-Acl -LiteralPath $temp
$sid = [Security.Principal.WindowsIdentity]::GetCurrent().User
$owner = $before.GetOwner([Security.Principal.SecurityIdentifier]).Value
$group = $before.GetGroup([Security.Principal.SecurityIdentifier]).Value
if ($owner -ne $sid.Value) { throw 'Unexpected temp owner' }
$allowed = @($sid, [Security.Principal.SecurityIdentifier]::new('S-1-5-18'), [Security.Principal.SecurityIdentifier]::new('S-1-5-32-544'))
# Check every pathname boundary, including the drive root. A private leaf
# cannot stop a parent's DELETE_CHILD grant, nor a replaceable ancestor.
$trusted = @($allowed | ForEach-Object { $_.Value })
$parent = [IO.DirectoryInfo]::new($env:HOH_PRIVATE_TEMP_PHYSICAL).Parent
while ($null -ne $parent) {
    if ($parent.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Reparse point' }
    $parentAcl = Get-Acl -LiteralPath $parent.FullName
    $descriptor = [Security.AccessControl.RawSecurityDescriptor]::new($parentAcl.GetSecurityDescriptorBinaryForm(), 0)
    if ($null -eq $descriptor.DiscretionaryAcl) { throw 'Unrestricted ancestor' }
    if ($parentAcl.GetOwner([Security.Principal.SecurityIdentifier]).Value -notin $trusted) { throw 'Unexpected ancestor owner' }
    foreach ($rule in $parentAcl.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier])) {
        if ($rule.AccessControlType -ne 'Allow' -or $rule.IdentityReference.Value -in $trusted -or
            ($rule.PropagationFlags -band [Security.AccessControl.PropagationFlags]::InheritOnly)) { continue }
        # WRITE_DAC/WRITE_OWNER can grant DELETE_CHILD later. DELETE permits
        # moving a non-root ancestor even when its descendants are private.
        $dangerous = 0x100C0040
        if ($null -ne $parent.Parent) { $dangerous = $dangerous -bor 0x10000 }
        if ([int]$rule.FileSystemRights -band $dangerous) { throw 'Unsafe ancestor permissions' }
    }
    $parent = $parent.Parent
}
if ($env:HOH_PRIVATE_TEMP_VERIFY_ONLY -ne '1') {
$acl = $directory.GetAccessControl([Security.AccessControl.AccessControlSections]::Access)
$acl.SetAccessRuleProtection($true, $false)
foreach ($rule in @($acl.GetAccessRules($true, $false, [Security.Principal.SecurityIdentifier]))) { $acl.RemoveAccessRuleAll($rule) }
foreach ($identity in $allowed) {
    $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($identity, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow'))
}
$directory.SetAccessControl($acl)
}
$verified = Get-Acl -LiteralPath $temp
$rules = @($verified.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]))
$expected = @($allowed | ForEach-Object { $_.Value } | Sort-Object -Unique)
$actual = @($rules | ForEach-Object { $_.IdentityReference.Value } | Sort-Object -Unique)
if (-not $verified.AreAccessRulesProtected -or $rules.Count -ne 3 -or
    @(Compare-Object $expected $actual).Count -or
    $verified.GetOwner([Security.Principal.SecurityIdentifier]).Value -ne $owner -or
    $verified.GetGroup([Security.Principal.SecurityIdentifier]).Value -ne $group -or
    @($rules | Where-Object { $_.IsInherited -or $_.AccessControlType -ne 'Allow' -or
        $_.FileSystemRights -ne [Security.AccessControl.FileSystemRights]::FullControl -or
        $_.InheritanceFlags -ne [Security.AccessControl.InheritanceFlags]'ContainerInherit,ObjectInherit' -or
        $_.PropagationFlags -ne [Security.AccessControl.PropagationFlags]::None }).Count -or
    ([IO.DirectoryInfo]::new($temp).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
    throw 'Private ACL verification failed'
}
'''
            helper_env = {k: v for k, v in os.environ.items()
                          if k.upper() != 'PSMODULEPATH'}
            helper_env['HOH_PRIVATE_TEMP'] = str(temp)
            helper_env['HOH_PRIVATE_TEMP_PHYSICAL'] = str(physical)
            helper_env['HOH_PRIVATE_TEMP_VERIFY_ONLY'] = '1' if explicit else '0'
            powershell = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
            result = subprocess.run([str(powershell), '-NoProfile', '-NonInteractive',
                '-EncodedCommand', base64.b64encode(script.encode('utf-16le')).decode('ascii')],
                env=helper_env, capture_output=True, timeout=30, creationflags=HIDDEN)
            if result.returncode:
                raise OSError('Private ACL setup failed')
    except (OSError, KeyError, ValueError, TypeError, subprocess.SubprocessError):
        # Helper output/environment may contain sensitive caller information.
        raise RuntimeError('Cannot establish private installation temporary directory') from None
    return temp


def environment(component):
    config = settings()
    env = {k: v for k, v in os.environ.items()
           if not k.upper().startswith(('HOH_', 'COV_CORE', 'COVERAGE'))
           and k.upper() not in {'PYTHONPATH', 'PYTHONHOME'}}
    temp = private_temp(config)
    env.update(TEMP=str(temp), TMP=str(temp))
    env.update(HOH_DATABASE_PATH=config['database_path'],
               HOH_WORKER_MODE='supervised', HOH_HOST='127.0.0.1',
               HOH_PORT=str(config['ui_port']), HOH_SEED_DEMO='0',
               HOH_MCP_HOST='127.0.0.1', HOH_MCP_PORT=str(config['mcp_port']),
               HOH_MCP_TRANSPORT='streamable-http', PYTHONIOENCODING='utf-8')
    if component == 'mcp':
        env['HOH_MCP_TOKENS_JSON'] = (ROOT / 'config/credentials.json').read_text(encoding='utf-8')
        env['HOH_LOCAL_CREDENTIALS_PATH'] = str(ROOT / 'config/agent-credentials.json')
    return env


def bootstrap_data():
    from hoh_control_plane.store import SQLiteStore
    from hoh_control_plane.service import ControlPlaneService
    import sqlite3
    database = Path(settings()['database_path'])
    if not database.is_file():
        raise RuntimeError('The existing database must be present before upgrading')
    backup_path = ROOT / 'config/control-plane-before-0.3.0.db'
    if backup_path.exists():
        raise RuntimeError('Upgrade backup already exists; do not overwrite it')
    with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True) as source:
        with sqlite3.connect(backup_path) as backup:
            source.backup(backup)
            if backup.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise RuntimeError('Backup integrity check failed')
    store = SQLiteStore(database)
    store.initialize()
    from hoh_control_plane.agent_kit import build_kit
    for role in ('planner', 'developer', 'qa', 'integrator'):
        build_kit(ROOT / 'kits' / role, role, f"http://127.0.0.1:{settings()['mcp_port']}/mcp")
    print('Verified database backup, applied additive schema upgrades and generated four agent kits. Existing data/worktrees retained in place.')


def component_main(component):
    # Each service gets a private, rotating log; HTTP access logs are disabled.
    handler = RotatingFileHandler(ROOT / 'logs' / (component + '.log'),
                                  maxBytes=2_000_000, backupCount=2, encoding='utf-8')
    logging.basicConfig(handlers=[handler], level=logging.WARNING,
                        format='%(asctime)s %(levelname)s %(message)s', force=True)
    if component == 'ui':
        import uvicorn
        from hoh_control_plane.desktop_control import create_desktop_app
        config = settings()
        app = create_desktop_app(ROOT)
        uvicorn.run(app, host='127.0.0.1', port=config['ui_port'], access_log=False, log_config=None)
    else:
        import importlib
        name = {'mcp': 'mcp_server', 'scheduler': 'scheduler', 'supervisor': 'supervisor'}[component]
        sys.argv = [sys.argv[0]] + (['--max-workers', str(settings()['max_workers'])] if component == 'supervisor' else [])
        importlib.import_module('hoh_control_plane.' + name).main()


def identity_matches():
    try:
        config = settings()
        with urlopen(f"http://127.0.0.1:{config['ui_port']}/__local_installation", timeout=2) as response:
            return json.load(response)['installation_id'] == config['installation_id']
    except Exception:
        return False


def host_main():
    from hoh_control_plane.desktop_control import prepare_desktop_start, shutdown_ready
    from hoh_control_plane.domain import ConflictError
    from hoh_control_plane.resource_lock import exclusive_file_lock
    from hoh_control_plane.managed_process import run_managed
    lock = exclusive_file_lock(ROOT / 'config/desktop-host.lock')
    try:
        lock.__enter__()
    except ConflictError:
        return  # A double-click reuses the owner already holding the OS lock.
    stopping = threading.Event()
    try:
        config = settings()
        for port in (config['ui_port'], config['mcp_port']):
            with socket.socket() as probe:
                probe.bind(('127.0.0.1', port))  # Refuse to displace another application.
        stop_file = ROOT / 'config/stop.request'
        prepare_desktop_start(ROOT)
        stop_file.unlink(missing_ok=True)
        logger = logging.getLogger('desktop-host')
        handler = RotatingFileHandler(ROOT / 'logs/desktop-host.log', maxBytes=1_000_000,
                                     backupCount=2, encoding='utf-8')
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)

        def run(component):
            failures = 0
            while not stopping.is_set():
                started = time.monotonic()
                try:
                    result = run_managed([str(ROOT / '.venv/Scripts/python.exe'), str(Path(__file__)),
                                          '--component', component], cwd=ROOT, env=environment(component),
                                         timeout=31_536_000, output_limit=20_000, cancel=stopping.is_set)
                    # Do not log raw subprocess output: it may contain credential-bearing exceptions.
                    logger.info('%s exited %s, cancelled=%s', component, result.returncode, result.cancelled)
                except Exception as error:
                    logger.warning('%s launcher error: %s', component, type(error).__name__)
                failures = 0 if time.monotonic() - started > 60 else min(failures + 1, 5)
                stopping.wait(min(2 ** failures, 30))

        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(run, component) for component in COMPONENTS]
            try:
                while not stop_file.exists():
                    status = {'pid': os.getpid(), 'heartbeat': time.time(), 'installation_id': config['installation_id']}
                    temp = ROOT / 'config/desktop-state.tmp'
                    temp.write_text(json.dumps(status), encoding='utf-8')
                    os.replace(temp, ROOT / 'config/desktop-state.json')
                    if shutdown_ready(ROOT):
                        stop_file.touch()
                    time.sleep(1)
            finally:
                stopping.set()
            for future in futures:
                future.result()
    finally:
        stopping.set()
        lock.__exit__(None, None, None)


def launch(open_browser=True):
    if not identity_matches():
        # Start-Process detaches the Windows GUI host from a short-lived console.
        # The host's OS lock arbitrates concurrent launch requests.
        powershell = str(Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe')
        subprocess.run([powershell, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                        '-File', str(ROOT / 'start-host.ps1')], cwd=ROOT, env=environment('host'),
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, creationflags=HIDDEN, check=True)
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        if identity_matches():
            if open_browser:
                webbrowser.open(f"http://127.0.0.1:{settings()['ui_port']}/?project={settings()['project_id']}")
            return
        time.sleep(0.5)
    raise RuntimeError('HoH could not start. Check D:\\HoH-service\\logs and local port availability.')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--initialize', action='store_true')
    parser.add_argument('--bootstrap-data', action='store_true')
    parser.add_argument('--host', action='store_true')
    parser.add_argument('--component', choices=COMPONENTS)
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--stop', action='store_true')
    args = parser.parse_args()
    if args.initialize:
        initialize()
    elif args.bootstrap_data:
        bootstrap_data()
    elif args.component:
        component_main(args.component)
    elif args.host:
        host_main()
    elif args.stop:
        (ROOT / 'config/stop.request').touch()
    else:
        launch(not args.no_browser)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # GUI launchers must show a visible failure without dumping secrets.
        if '--host' not in sys.argv and '--component' not in sys.argv:
            ctypes.windll.user32.MessageBoxW(None, 'HoH startup failed. See D:\\HoH-service\\logs.\n' + type(error).__name__, 'HoH', 0x10)
        raise
