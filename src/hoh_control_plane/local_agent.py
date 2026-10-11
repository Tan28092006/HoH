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
MODEL_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:/\[\]-]{0,99}')
MODEL_FAMILIES = ('anthropic', 'openai', 'google', 'other')


def model_family_of(model):
    """The provider family of a model id, for agent.register(model_family=...).

    Checker/maker distinctness compares families, so an unrecognised id is
    'other', which the server treats as unverified rather than as distinct.
    """
    name = (model or '').lower().rsplit('/', 1)[-1]
    if not name:
        return None
    if name.startswith(('claude', 'opus', 'sonnet', 'haiku', 'fable')) or 'anthropic' in (model or '').lower():
        return 'anthropic'
    if name.startswith(('gpt', 'o1', 'o3', 'o4', 'codex', 'chatgpt')):
        return 'openai'
    if name.startswith(('gemini', 'gemma')):
        return 'google'
    return 'other'


def model_profile_path(root, agent_id):
    return Path(root) / 'config' / 'agent_models' / f'{agent_id}.json'


def worker_agent_text(server, role, model, model_family=None):
    """A Claude Code subagent that works one claimed ticket on its own model.

    Claude Code has no model setting per MCP server, so the model a role's work
    runs on is pinned on the subagent the dispatcher delegates each ticket to.
    """
    return (
        f"---\nname: {server}-worker\n"
        f"description: Works one claimed HoH {role} ticket from the claim's worker_brief "
        f"and returns only the brief's result fields. Use when the hoh-{role} dispatcher "
        f"delegates a claimed ticket.\nmodel: {model}\n---\n\n"
        f"You are a fresh HoH {role} worker for exactly one ticket. Load the skills the "
        f"brief names, use only MCP server `{server}`, and start with the brief's "
        f"`next_calls`. Work only that ticket and lease. Finish by returning only the "
        f"brief's `returns` fields plus any blockers, nothing from your transcript.\n"
        + (f"Your model family is `{model_family}`; the dispatcher registers it with "
           f"`agent.register(model_family=\"{model_family}\", model=\"{model}\")`.\n"
           if model_family else "")
    )


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


def install_profile(root, project, role, instance, client, home, upgrade_skills=False, transport='stdio',
                    model=None, model_family=None, rotate_token=False):
    from hoh_control_plane.agent_kit import build_kit
    from hoh_control_plane.agent_assets.install import install
    config = checked_project(root, project)
    if transport not in {'stdio', 'shared-http'}:
        raise ValueError('Unknown local MCP transport')
    if model is not None and not MODEL_ID.fullmatch(model):
        raise ValueError('Model must be a plain model identifier')
    if model_family is None:
        model_family = model_family_of(model)
    elif model_family not in MODEL_FAMILIES:
        raise ValueError('Model family must be one of ' + ', '.join(MODEL_FAMILIES))
    actor = installed_profile_actor(root, config, project, role, instance)
    server = 'hoh-' + actor.agent_id
    command = str(root / '.venv/Scripts/python.exe')
    arguments = ['-m', 'hoh_control_plane.local_agent', 'serve', '--root', str(root),
                 '--project', project, '--role', role, '--instance', instance]
    codex = {'command': command, 'args': arguments, 'tool_timeout_sec': 86400}
    claude = {'type': 'stdio', 'command': command, 'args': arguments, 'timeout': 86400000}
    previous = {'codex': codex, 'claude-code': claude}
    token_var = token_issued = None
    if transport == 'shared-http':
        from hoh_control_plane.local_credentials import (
            deliver_token, register_local_credential, token_delivery_available, token_env_var)
        if not token_delivery_available():
            raise ValueError('shared-http profiles deliver their token through the Windows user '
                             'environment; use the stdio transport on this host')
        # The client config names an environment variable, never the token
        # (wave 8A, R4); the credential file keeps only its digest.
        token_var = token_env_var(actor.agent_id)
        url = f"http://127.0.0.1:{int(config.get('mcp_port', 8931))}/mcp"
        codex = {'url': url, 'bearer_token_env_var': token_var, 'tool_timeout_sec': 86400}
        claude = {'type': 'http', 'url': url, 'headers': {'Authorization': 'Bearer ${' + token_var + '}'},
                  'timeout': 86400000}
    with tempfile.TemporaryDirectory(prefix='hoh-profile-') as temp:
        kit = Path(temp) / 'kit'
        build_kit(kit, role, 'http://127.0.0.1:8931/mcp')
        if transport == 'shared-http':
            codex_text = (f'[mcp_servers.{server}]\nurl = {json.dumps(url)}\n'
                          f'bearer_token_env_var = {json.dumps(token_var)}\ntool_timeout_sec = 86400\n')
        else:
            codex_text = f'[mcp_servers.{server}]\ncommand = {json.dumps(command)}\nargs = {json.dumps(arguments)}\ntool_timeout_sec = 86400\n'
        (kit / 'codex.toml').write_text(codex_text, encoding='utf-8')
        (kit / 'mcp.json').write_text(json.dumps({'mcpServers': {server: claude}}), encoding='utf-8')
        manifest = json.loads((kit / 'manifest.json').read_text(encoding='utf-8'))
        manifest['server_name'], manifest['codex'] = server, codex
        if model_family:
            manifest['model_family'] = model_family
            manifest['model'] = model
        model_note = None
        if model and client == 'codex':
            # A Codex profile carries a model; it applies when Codex starts with
            # --profile <server>.
            manifest['codex_profile'] = {'name': server, 'model': model}
        elif model and role != 'planner':
            agent = f'agents/{server}-worker.md'
            (kit / 'agents').mkdir()
            (kit / agent).write_text(worker_agent_text(server, role, model, model_family), encoding='utf-8')
            manifest['claude_agent'] = agent
        elif model:
            model_note = ('Claude Code has no per-server model setting; choose the Planner '
                          'chat model with /model or claude --model.')
        if transport == 'shared-http':
            manifest['upgrade_from'] = previous
            from hoh_control_plane.auth import credentials_from_environment
            import tomllib
            credential_path = root / 'config/agent-credentials.json'
            credentials = credentials_from_environment(credential_path.read_text(encoding='utf-8')) if credential_path.exists() else ()
            digests = [c.digest for c in credentials if c.actor == actor]
            manifest['http_upgrade'] = {'url': url, 'digests': digests}
            config_path = home / ('.codex/config.toml' if client == 'codex' else '.claude.json')
            recovery_token = None
            if config_path.exists():
                raw = config_path.read_text(encoding='utf-8')
                parsed = tomllib.loads(raw) if client == 'codex' else json.loads(raw)
                old = parsed.get('mcp_servers' if client == 'codex' else 'mcpServers', {}).get(server, {})
                authorization = old.get('http_headers' if client == 'codex' else 'headers', {}).get('Authorization', '')
                if old.get('url') == url and authorization.startswith('Bearer '):
                    from hoh_control_plane.auth import token_digest
                    value = authorization[7:]
                    if value.isascii() and token_digest(value) in digests:
                        recovery_token = value
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
            install(kit, home, client, dry_run=True)
            if transport == 'shared-http':
                token = register_local_credential(
                    root, actor, rotate=rotate_token, delivery=deliver_token,
                    apply=lambda: install(kit, home, client), recovery_token=recovery_token)
                token_issued = token is not None
            else:
                install(kit, home, client)
        except BaseException:
            for destination, backup in reversed(moved):
                if not destination.exists():
                    shutil.move(str(backup), destination)
            raise
    result = {'server_name': server, 'agent_id': actor.agent_id, 'role': role, 'project_id': project, 'transport': transport,
              'skill_backups': [str(backup) for _, backup in moved]}
    if token_var:
        # The value is never reported; a new token reaches clients started afterwards.
        result['token_env_var'] = token_var
        result['token_issued'] = token_issued
    if model_family:
        # A stdio server reads this at start and registers the family when the
        # agent omits it; every client also gets the explicit register hint.
        path = model_profile_path(root, actor.agent_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'model_family': model_family, 'model': model}), encoding='utf-8')
        result['model_family'] = model_family
        result['register_hint'] = (f'agent.register(..., model_family="{model_family}"'
                                   + (f', model="{model}")' if model else ')'))
    if model:
        result['model'] = model
        if client == 'codex':
            result['codex_profile'] = server
        elif role != 'planner':
            result['claude_agent'] = f'{server}-worker'
        else:
            result['model_note'] = model_note
    return result


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
    dispatch = (
        " This chat is a thin dispatcher: after each claim, pass the claim's "
        '`worker_brief` verbatim to a fresh subagent (Claude Code Agent/Task tool, '
        'Codex `spawn_agent`) and keep only its structured result, never its '
        'transcript. Without subagents, start a new session per ticket. Report each '
        'ticket with `work.report_usage`; on `advice: start_new_session` start a new chat.'
    )
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
5. {focus[role]}{dispatch if role != 'planner' else ''}
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
    parser.add_argument('--model', help='Model this role runs on, where the client supports it')
    parser.add_argument('--model-family', choices=MODEL_FAMILIES,
                        help='Provider family of --model; derived from the model id when omitted')
    parser.add_argument('--rotate-token', action='store_true',
                        help='Issue a new shared-http token for this profile, revoking the old one')
    args = parser.parse_args()
    root = args.root.resolve()
    config = checked_project(root, args.project)
    actor = installed_profile_actor(root, config, args.project, args.role, args.instance)
    if args.action == 'setup':
        print(json.dumps(install_profile(root, args.project, args.role, args.instance, args.client, args.home, args.upgrade_skills, args.transport,
                                         args.model, args.model_family, args.rotate_token)))
        return
    if any((root / 'config' / name).exists() for name in ('stop.request', 'shutdown.request')):
        raise SystemExit('HoH was stopped by the operator. Open the HoH desktop icon before reconnecting.')
    os.environ.pop('HOH_DATABASE_URL', None)
    os.environ.pop('PYTHONPATH', None)
    os.environ.pop('PYTHONHOME', None)
    os.environ.update(HOH_DATABASE_PATH=config['database_path'], HOH_WORKER_MODE='supervised',
                      HOH_MCP_TRANSPORT='stdio', HOH_AGENT_ID=actor.agent_id,
                      HOH_AGENT_ROLE=args.role, HOH_PROJECT_IDS=args.project)
    model_profile = model_profile_path(root, actor.agent_id)
    if model_profile.is_file():
        recorded = json.loads(model_profile.read_text(encoding='utf-8'))
        os.environ['HOH_AGENT_MODEL_FAMILY'] = recorded.get('model_family') or ''
        os.environ['HOH_AGENT_MODEL'] = recorded.get('model') or ''
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
