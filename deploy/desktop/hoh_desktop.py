"""Desktop launcher for the isolated local HoH installation (stdlib + HoH)."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import ctypes
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import secrets
import socket
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


def environment(component):
    config = settings()
    env = {k: v for k, v in os.environ.items()
           if not k.upper().startswith(('HOH_', 'COV_CORE', 'COVERAGE'))
           and k.upper() not in {'PYTHONPATH', 'PYTHONHOME'}}
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
