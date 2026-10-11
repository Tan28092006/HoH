"""Migrate recognized local HoH profiles to the shared HTTP host atomically."""
import argparse
import json
from pathlib import Path
import re
import tomllib

from hoh_control_plane.domain import Actor, Role
from hoh_control_plane.auth import credentials_from_environment, token_digest
from hoh_control_plane.local_agent import checked_project, profile_actor
from hoh_control_plane.local_credentials import (
    deliver_token, register_local_credential, token_delivery_available, token_env_var)


def recognized_actor(name, profile, root, legacy_repository=None):
    arguments = profile.get('args', [])
    command = profile.get('command', '')
    if not isinstance(arguments, list) or not all(isinstance(arg, str) for arg in arguments):
        return None
    if command == str(root / '.venv/Scripts/python.exe') and arguments[:3] == ['-m', 'hoh_control_plane.local_agent', 'serve']:
        rest = arguments[3:]
        if len(rest) != 8 or rest[::2] != ['--root', '--project', '--role', '--instance']:
            return None
        if Path(rest[1]).resolve() != root:
            return None
        actor = profile_actor(rest[3], rest[5], rest[7])
        if name != 'hoh-' + actor.agent_id or profile.get('env'):
            return None
        checked_project(root, rest[3])
        return actor
    if legacy_repository is not None and name in {'hoh-planner', 'hoh-developer', 'hoh-qa', 'hoh-integrator'}:
        expected = ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(legacy_repository / 'scripts/start-mcp.ps1')]
        if command.lower() != 'powershell.exe' or arguments != expected:
            return None
        env = profile.get('env', {})
        if any(not key.startswith('HOH_') for key in env):
            return None
        role = Role(env['HOH_AGENT_ROLE'])
        if name != 'hoh-' + role.value:
            return None
        projects = frozenset(item.strip() for item in env.get('HOH_PROJECT_IDS', '').split(',') if item.strip())
        if not projects or '*' in projects:
            return None
        for project in projects:
            checked_project(root, project)
        return Actor(env['HOH_AGENT_ID'], role, projects)
    return None


def recognized_http_actor(name, profile, root, client):
    """Authenticate a literal legacy profile against this host's exact actor map."""
    config = json.loads((root / 'config/settings.json').read_text(encoding='utf-8'))
    url = f"http://127.0.0.1:{int(config['mcp_port'])}/mcp"
    key = 'http_headers' if client == 'codex' else 'headers'
    headers = profile.get(key, {})
    authorization = headers.get('Authorization', '')
    allowed = {'url', key, 'type', 'tool_timeout_sec', 'startup_timeout_sec', 'bearer_token_env_var',
               'enabled', 'required', 'enabled_tools', 'disabled_tools', 'timeout'}
    if (profile.get('url') != url or set(profile) - allowed
            or (client != 'codex' and 'bearer_token_env_var' in profile)
            or (('type' in profile) if client == 'codex' else profile.get('type') != 'http')):
        raise ValueError('Unrecognized shared HTTP profile; review before migration')
    variable = profile.get('bearer_token_env_var') if client == 'codex' else None
    reference = re.fullmatch(r'Bearer \$\{([A-Za-z_][A-Za-z0-9_]*)\}', authorization)
    if client != 'codex' and reference and set(headers) == {'Authorization'}:
        variable = reference[1]
    if variable:
        if client == 'codex' and headers:
            raise ValueError('Unrecognized shared HTTP profile; review before migration')
        from hoh_control_plane.local_credentials import read_token
        try:
            token = read_token(variable)
        except Exception:
            raise RuntimeError('Credential recovery needed: user token variable could not be read') from None
    elif isinstance(authorization, str) and authorization.startswith('Bearer ') and set(headers) == {'Authorization'}:
        token = authorization[7:]
    else:
        raise ValueError('Unrecognized shared HTTP profile; review before migration')
    if not isinstance(token, str) or len(token) < 32 or not token.isascii() or any(c.isspace() for c in token):
        raise RuntimeError('Credential recovery needed: profile token variable is missing or invalid')
    credential_path = root / 'config/agent-credentials.json'
    credentials = credentials_from_environment(credential_path.read_text(encoding='utf-8')) if credential_path.exists() else ()
    matches = [c.actor for c in credentials if c.digest == token_digest(token)
               and name in {'hoh-' + c.actor.agent_id, 'hoh-' + c.actor.role.value}]
    if len(matches) != 1:
        raise ValueError('Unrecognized shared HTTP credential; review before migration')
    if variable and variable != token_env_var(matches[0].agent_id):
        raise ValueError('Unrecognized shared HTTP token variable; review before migration')
    for project in matches[0].project_ids:
        checked_project(root, project)
    return matches[0], token, bool(variable)


def replace_codex_table(text, name, profile):
    # Locate the exact generated table plus its env subtables. No broad regex
    # across other MCP servers, projects, model settings or comments.
    headers = list(re.finditer(r'(?m)^\[([^\]\r\n]+)\][ \t]*$', text))
    target = 'mcp_servers.' + name
    matches = [i for i, match in enumerate(headers) if match[1] == target]
    if len(matches) != 1:
        raise ValueError('Cannot locate recognized HoH profile')
    index = matches[0]
    end = len(text)
    for match in headers[index + 1:]:
        if not match[1].startswith(target + '.'):
            end = match.start()
            break
    lines = [f'[{target}]']
    for key, value in profile.items():
        encoded = json.dumps(value)
        lines.append(f'{key} = {encoded}')
    segment = text[headers[index].start():end]
    trailing = re.search(r'\n(?:[ \t]*(?:#[^\n]*)?\n)*\Z', segment)
    suffix = trailing.group(0) if trailing else '\n\n'
    return text[:headers[index].start()] + '\n'.join(lines) + suffix + text[end:]


def migrate(root, home, client, legacy_repository=None, apply=False):
    root = Path(root).resolve()
    legacy_repository = Path(legacy_repository).resolve() if legacy_repository else None
    path = Path(home) / ('.codex/config.toml' if client == 'codex' else '.claude.json')
    original = path.read_text(encoding='utf-8')
    parsed = tomllib.loads(original) if client == 'codex' else json.loads(original)
    profiles = parsed.get('mcp_servers' if client == 'codex' else 'mcpServers', {})
    credential_path = root / 'config/agent-credentials.json'
    credentials = credentials_from_environment(credential_path.read_text(encoding='utf-8')) if credential_path.exists() else ()
    variables = {}
    for credential in credentials:
        variable = token_env_var(credential.actor.agent_id)
        previous = variables.setdefault(variable, credential.actor)
        if previous != credential.actor:
            raise ValueError('Credential token variable collision; review identities before migration')
    text = original
    changed = []
    deliveries = []
    for name, profile in profiles.items():
        if not name.startswith('hoh-'):
            continue
        recovery_token = None
        if 'url' in profile:
            actor, recovery_token, converted = recognized_http_actor(name, profile, root, client)
            if converted:
                continue
        else:
            actor = recognized_actor(name, profile, root, legacy_repository)
        if actor is None:
            continue
        # Preserve user choices that are valid for HTTP. Unknown settings need
        # review rather than silently disappearing during transport conversion.
        allowed = {'command', 'args', 'env', 'type', 'tool_timeout_sec', 'startup_timeout_sec',
                   'enabled', 'required', 'enabled_tools', 'disabled_tools', 'timeout'}
        if recovery_token:
            allowed |= {'url', 'http_headers', 'headers'}
        if set(profile) - allowed:
            raise ValueError(f'Profile {name} has custom settings requiring review')
        changed.append({'server_name': name, 'agent_id': actor.agent_id, 'role': actor.role.value,
                        'project_ids': sorted(actor.project_ids)})
        if not apply:
            continue
        if not token_delivery_available():
            raise ValueError('shared-http profiles deliver their token through the Windows user environment')
        # The profile names an environment variable, never the token (wave 8A, R4).
        token_var = token_env_var(actor.agent_id)
        deliveries.append((actor, recovery_token))
        config = json.loads((root / 'config/settings.json').read_text(encoding='utf-8'))
        updated = {key: value for key, value in profile.items() if key not in {'command', 'args', 'env', 'type', 'http_headers', 'headers'}}
        updated['url'] = f"http://127.0.0.1:{int(config['mcp_port'])}/mcp"
        if client == 'codex':
            updated['bearer_token_env_var'] = token_var
            updated['tool_timeout_sec'] = max(updated.get('tool_timeout_sec', 86400), 86400)
            text = replace_codex_table(text, name, updated)
        else:
            updated.update(type='http', headers={'Authorization': 'Bearer ${' + token_var + '}'})
            updated['timeout'] = max(updated.get('timeout', 86400000), 86400000)
            profiles[name] = updated
    backup = None
    if apply and changed:
        if client != 'codex':
            text = json.dumps(parsed, indent=2) + '\n'
        else:
            tomllib.loads(text)
        if path.read_text(encoding='utf-8') != original:
            raise RuntimeError('Client configuration changed during migration; retry after it settles')
        # Even stdio or unrelated settings may hold secrets: never back up raw input.
        from hoh_control_plane.agent_assets.install import _config_lock, _replace_atomically
        with _config_lock(path):
            if path.read_text(encoding='utf-8') != original:
                raise RuntimeError('Client configuration changed before delivery; retry after it settles')
        # Setup takes credential then config locks. Never nest the reverse order.
        for actor, recovery_token in deliveries:
            register_local_credential(root, actor, delivery=deliver_token, recovery_token=recovery_token)
        with _config_lock(path):
            if path.read_text(encoding='utf-8') != original:
                raise RuntimeError('Client configuration changed before write; retry after it settles')
            _replace_atomically(path, text)
    return {'client': client, 'applied': apply, 'profiles': changed, 'backup_path': backup}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--home', type=Path, default=Path.home())
    parser.add_argument('--client', choices=['codex', 'claude-code'], required=True)
    parser.add_argument('--legacy-repository', type=Path)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    print(json.dumps(migrate(args.root, args.home, args.client, args.legacy_repository, args.apply)))


if __name__ == '__main__':
    main()
