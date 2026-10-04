"""Project-scoped stdio profiles for agents running on the desktop host."""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time

from hoh_control_plane.domain import Actor, Role, NotFoundError

ROLES = ('planner', 'developer', 'qa', 'integrator')


def profile_actor(project, role, instance, salt=None):
    """Derive a profile identity.

    With a salt (the installation id) the identity is unique to this HoH
    installation, so two machines that share a Git remote and happen to use the
    same project, role and instance names never produce the same branch or
    worktree names. Without a salt the legacy derivation is kept, because
    identities registered before the salt existed must keep resolving.
    """
    if role not in ROLES or not re.fullmatch(r'[A-Za-z0-9._-]{1,80}', project) or not re.fullmatch(r'[A-Za-z0-9_-]{1,40}', instance):
        raise ValueError('Invalid project, role or instance')
    if salt:
        digest = hashlib.sha256((str(salt) + ':' + project + ':' + role + ':' + instance).encode()).hexdigest()[:8]
    else:
        digest = hashlib.sha256((project + ':' + role + ':' + instance).encode()).hexdigest()[:16]
    return Actor(f'local-{role}-{digest}', Role(role), frozenset({project}))


def installed_profile_actor(root, config, project, role, instance):
    """The identity a profile has on this installation.

    A profile registered before identities were salted keeps its legacy id, so
    an existing chat's credential, leases and worktrees stay its own; a new
    profile is salted with the installation id.
    """
    from hoh_control_plane.auth import credentials_from_environment
    legacy = profile_actor(project, role, instance)
    path = Path(root) / 'config/agent-credentials.json'
    if path.exists():
        for credential in credentials_from_environment(path.read_text(encoding='utf-8')):
            if credential.actor == legacy:
                return legacy
    database = (config or {}).get('database_path')
    if database:
        with closing(sqlite3.connect(Path(database).resolve().as_uri() + '?mode=ro', uri=True)) as db:
            if db.execute('SELECT 1 FROM agent_presence WHERE agent_id=?', (legacy.agent_id,)).fetchone():
                return legacy
    salt = (config or {}).get('installation_id')
    return profile_actor(project, role, instance, salt) if salt else legacy


def checked_project(root, project):
    config = json.loads((root / 'config/settings.json').read_text(encoding='utf-8'))
    database = Path(config['database_path']).resolve()
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as db:
        row = db.execute('SELECT name FROM projects WHERE project_id = ?', (project,)).fetchone()
    if row is None:
        raise NotFoundError('Project does not exist on this HoH host')
    return config


def install_profile(root, project, role, instance, client, home, upgrade_skills=False, transport='stdio'):
    from hoh_control_plane.agent_kit import build_kit
    from hoh_control_plane.agent_assets.install import install
    config = checked_project(root, project)
    if transport not in {'stdio', 'shared-http'}:
        raise ValueError('Unknown local MCP transport')
    actor = installed_profile_actor(root, config, project, role, instance)
    server = 'hoh-' + actor.agent_id
    command = str(root / '.venv/Scripts/python.exe')
    arguments = ['-m', 'hoh_control_plane.local_agent', 'serve', '--root', str(root),
                 '--project', project, '--role', role, '--instance', instance]
    codex = {'command': command, 'args': arguments, 'tool_timeout_sec': 86400}
    claude = {'type': 'stdio', 'command': command, 'args': arguments, 'timeout': 86400000}
    previous = {'codex': codex, 'claude-code': claude}
    if transport == 'shared-http':
        from hoh_control_plane.local_credentials import register_local_credential
        token = register_local_credential(root, actor)
        url = f"http://127.0.0.1:{int(config.get('mcp_port', 8931))}/mcp"
        headers = {'Authorization': 'Bearer ' + token}
        codex = {'url': url, 'http_headers': headers, 'tool_timeout_sec': 86400}
        claude = {'type': 'http', 'url': url, 'headers': headers, 'timeout': 86400000}
    with tempfile.TemporaryDirectory(prefix='hoh-profile-') as temp:
        kit = Path(temp) / 'kit'
        build_kit(kit, role, 'http://127.0.0.1:8931/mcp')
        if transport == 'shared-http':
            codex_text = (f'[mcp_servers.{server}]\nurl = {json.dumps(url)}\ntool_timeout_sec = 86400\n'
                          f'http_headers = {{ Authorization = {json.dumps(headers["Authorization"])} }}\n')
        else:
            codex_text = f'[mcp_servers.{server}]\ncommand = {json.dumps(command)}\nargs = {json.dumps(arguments)}\ntool_timeout_sec = 86400\n'
        (kit / 'codex.toml').write_text(codex_text, encoding='utf-8')
        (kit / 'mcp.json').write_text(json.dumps({'mcpServers': {server: claude}}), encoding='utf-8')
        manifest = json.loads((kit / 'manifest.json').read_text(encoding='utf-8'))
        manifest['server_name'], manifest['codex'] = server, codex
        if transport == 'shared-http':
            manifest['upgrade_from'] = previous
        manifest['files'] = {p.relative_to(kit).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                             for p in kit.rglob('*') if p.is_file() and p.name != 'manifest.json'}
        (kit / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
        moved = []
        try:
            if upgrade_skills:
                skill_root = home / ('.codex/skills' if client == 'codex' else '.claude/skills')
                for source in (kit / 'skills').iterdir():
                    destination = skill_root / source.name
                    if not destination.exists():
                        continue
                    if not destination.resolve().is_relative_to(skill_root.resolve()) or destination.is_symlink() or destination.is_junction():
                        raise ValueError('Refusing to replace a linked skill directory')
                    contents = lambda path: {p.relative_to(path): p.read_bytes() for p in path.rglob('*') if p.is_file()}
                    if contents(source) != contents(destination):
                        backup_root = home / '.hoh-skill-backups'
                        backup_root.mkdir(exist_ok=True)
                        backup = Path(tempfile.mkdtemp(prefix=source.name + '-', dir=backup_root)) / source.name
                        shutil.move(str(destination), backup)
                        moved.append((destination, backup))
            install(kit, home, client)
        except BaseException:
            for destination, backup in reversed(moved):
                if not destination.exists():
                    shutil.move(str(backup), destination)
            raise
    return {'server_name': server, 'agent_id': actor.agent_id, 'role': role, 'project_id': project, 'transport': transport,
            'skill_backups': [str(backup) for _, backup in moved]}


def make_prompts(root, project, instance='primary'):
    """Generate the short operational prompt; detailed policy lives in skills."""
    root_path = Path(root)
    service_root = root_path.as_posix()
    python = (root_path / '.venv' / 'Scripts' / 'python.exe').as_posix()
    focus = {
        'planner': (
            'Run the Planner card after bootstrap and every event: sync repository, '
            'resolve reconciliation before any release/wait, handle Planner jobs and '
            'capability reviews, persist current priorities, then wait. Read the linked '
            'reference only for the event you received.'
        ),
        'developer': (
            'Resume owned lease before new work, implement only in the assigned '
            'worktree, refresh context immediately before one-commit submission, and '
            'stop for a reconciliation barrier or retirement.'
        ),
        'qa': (
            'Declare only verified capabilities on this machine, inspect capability '
            'blockers before reporting no work, test only a frozen claimable candidate, '
            'and record evidence without modifying candidate code or tests.'
        ),
        'integrator': (
            'Merge only the exact verified candidate through the protected job. Verify '
            'the actual mainline SHA after completion and stop for Planner reconciliation.'
        ),
    }
    prompts = {}
    for role in ROLES:
        actor = installed_profile_actor(root, checked_project(Path(root), project), project, role, instance)
        prompts[role] = f'''Set up HoH for this Windows chat.
Role: {role}; project: {project}; instance: {instance}.

1. Identify Codex or Claude Code, then run the command in the block below exactly
   once. Copy it from the block rather than retyping it, and do not guess another
   executable or run setup twice.

   ```powershell
   & '{python}' -I -m hoh_control_plane.local_agent setup --root '{service_root}' --project '{project}' --role {role} --instance {instance} --client codex --upgrade-skills --transport shared-http
   ```

   For Claude Code, replace only `--client codex` with `--client claude-code`. If
   it fails with a ModuleNotFoundError whose module name contains backslashes,
   your client escaped the underscores: delete them and run it once more. Landing
   the same command intact is not a repeat of setup, so do not stop to ask.
2. Load `$hoh-control-plane` and `$hoh-{role}`. Use only MCP
   `hoh-{actor.agent_id}`. If it is absent after setup, keep HoH running and
   fully reopen Codex Desktop, or reload the VS Code extension host; do not use a
   default or different-role server.
3. Call `session.bootstrap(project_id="{project}")`. Confirm role, agent ID,
   project scope and selected project exactly match this prompt. If not, stop
   before registration, claim or write.
4. Register `hoh-control-plane` and `hoh-{role}` once, then inspect inbox/current
   assignment. QA uses the stable host name as `machine_id` and declares only
   equipment it has actually verified. Keep this same instance after restart;
   bootstrap and use `resume_wait_id` instead of creating a duplicate instance.
5. {focus[role]}
6. Use current server packet and exposed tools only. Never use direct SQL, another
   worker's worktree, hidden evaluator material or credentials in chat. Before
   idle wait, report one unhandled durable failure only if no role tool already
   opened a Planner job.
7. Once current work is handled, wait with `work.wait_for_event` as its tool
   description says; resume with `resume_wait_id`.

At setup completion, state the client, exact MCP server, role, project, workspace
model and next action briefly.'''
    return prompts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['setup', 'serve'])
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--project', required=True)
    parser.add_argument('--role', required=True, choices=ROLES)
    parser.add_argument('--instance', default='primary')
    parser.add_argument('--client', choices=['codex', 'claude-code'], default='codex')
    parser.add_argument('--home', type=Path, default=Path.home())
    parser.add_argument('--upgrade-skills', action='store_true')
    parser.add_argument('--transport', choices=['stdio', 'shared-http'], default='shared-http')
    args = parser.parse_args()
    root = args.root.resolve()
    config = checked_project(root, args.project)
    actor = installed_profile_actor(root, config, args.project, args.role, args.instance)
    if args.action == 'setup':
        print(json.dumps(install_profile(root, args.project, args.role, args.instance, args.client, args.home, args.upgrade_skills, args.transport)))
        return
    if any((root / 'config' / name).exists() for name in ('stop.request', 'shutdown.request')):
        raise SystemExit('HoH was stopped by the operator. Open the HoH desktop icon before reconnecting.')
    os.environ.pop('HOH_DATABASE_URL', None)
    os.environ.pop('PYTHONPATH', None)
    os.environ.pop('PYTHONHOME', None)
    os.environ.update(HOH_DATABASE_PATH=config['database_path'], HOH_WORKER_MODE='supervised',
                      HOH_MCP_TRANSPORT='stdio', HOH_AGENT_ID=actor.agent_id,
                      HOH_AGENT_ROLE=args.role, HOH_PROJECT_IDS=args.project)
    # Start the supervised host while reserving stdout for MCP protocol frames.
    subprocess.run([sys.executable, str(root / 'hoh_desktop.py'), '--no-browser'],
                   stdout=subprocess.DEVNULL, check=True, creationflags=0x08000000 if os.name == 'nt' else 0)
    def watch_stop():
        while True:
            if (root / 'config/stop.request').exists():
                os._exit(0)
            time.sleep(1)
    threading.Thread(target=watch_stop, daemon=True).start()
    from hoh_control_plane.mcp_server import main as serve
    serve()


if __name__ == '__main__':
    main()
