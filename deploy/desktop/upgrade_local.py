"""Upgrade an existing desktop installation using a checksummed local bundle."""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import time
from urllib.error import URLError
from urllib.request import Request, urlopen


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True, type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    bundle = Path(__file__).resolve().parent
    manifest = json.loads((bundle / 'manifest.json').read_text(encoding='utf-8'))
    for name, expected in manifest['files'].items():
        path = (bundle / name).resolve()
        if not path.is_relative_to(bundle) or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise RuntimeError('Bundle checksum mismatch')
    config = json.loads((root / 'config/settings.json').read_text(encoding='utf-8'))
    python = root / '.venv/Scripts/python.exe'
    wheel = bundle / manifest['wheel']
    if (manifest['wheel'] not in manifest['files'] or not wheel.resolve().is_relative_to(bundle)
            or wheel.suffix != '.whl' or not python.is_file()):
        raise RuntimeError('Invalid wheel or installation runtime')
    backup = root / 'config' / ('upgrade-' + manifest['version'] + '-' + time.strftime('%Y%m%d-%H%M%S'))
    backup.mkdir(exist_ok=False)
    base = f"http://127.0.0.1:{config['ui_port']}"
    try:
        with urlopen(base + '/__local_installation', timeout=3) as response:
            if json.load(response)['installation_id'] != config['installation_id']:
                raise RuntimeError('The UI port belongs to another installation')
        with urlopen(base + '/local/session', timeout=3) as response:
            session = json.load(response)
        request = Request(base + '/local/shutdown', data=b'', method='POST',
                          headers={'Origin':base, 'X-HoH-Local-Token':session['csrf_token']})
        with urlopen(request, timeout=5) as response:
            if response.status != 202:
                raise RuntimeError('Host did not accept draining shutdown')
        print('Draining current workers and stopping the host.', flush=True)
    except URLError:
        # An already stopped host is allowed; both ports must be closed below.
        pass
    deadline = time.monotonic() + 60
    while True:
        listening = False
        for port in (config['ui_port'], config['mcp_port']):
            with socket.socket() as connection:
                connection.settimeout(.2)
                listening |= connection.connect_ex(('127.0.0.1', port)) == 0
        if not listening:
            break
        if time.monotonic() > deadline:
            raise RuntimeError('Host is still draining; installation was not changed')
        time.sleep(.5)
    database = Path(config['database_path']).resolve()
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as source:
        with closing(sqlite3.connect(backup / 'control-plane.db')) as target:
            source.backup(target)
            if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise RuntimeError('Database backup failed integrity check')
    shutil.copy2(root / 'hoh_desktop.py', backup / 'hoh_desktop.py')
    environment = {k:v for k,v in os.environ.items()
                   if k.upper() not in {'PYTHONPATH', 'PYTHONHOME'} and not k.upper().startswith('HOH_')}
    subprocess.run([str(python), '-I', '-m', 'pip', 'install', '--no-deps', '--force-reinstall', str(wheel)],
                   env=environment, check=True)
    shutil.copy2(bundle / 'hoh_desktop.py', root / 'hoh_desktop.py')
    subprocess.run([str(python), '-I', str(root / 'hoh_desktop.py'), '--no-browser'], env=environment, check=True)
    with urlopen(base + '/__local_installation', timeout=5) as response:
        state = json.load(response)
    if state['version'] != manifest['version'] or state['installation_id'] != config['installation_id']:
        raise RuntimeError('Installed host identity/version verification failed')
    print(json.dumps({'version':state['version'], 'backup':str(backup), 'status':'healthy'}))


if __name__ == '__main__':
    main()
