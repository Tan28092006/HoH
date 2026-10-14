"""Read-only bootstrap verification and optional concurrent-client resource probe."""
import argparse
import asyncio
from contextlib import AsyncExitStack
import json
import os
import re
from pathlib import Path
import tomllib

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


def profile_headers(profile):
    headers = dict(profile.get('http_headers', profile.get('headers', {})))
    variable = profile.get('bearer_token_env_var')
    reference = re.fullmatch(r'Bearer \$\{([A-Za-z_][A-Za-z0-9_]*)\}', headers.get('Authorization', ''))
    if reference:
        variable = reference[1]
    if variable:
        token = os.environ.get(variable)
        if not token or not token.isascii() or any(c.isspace() for c in token):
            raise RuntimeError('Profile token environment variable is missing or invalid')
        headers['Authorization'] = 'Bearer ' + token
    return headers


async def verify(home, connections, hold, project, servers=None, clients=None):
    profiles = []
    clients = clients or ['codex', 'claude-code']
    for client, path in [('codex', home / '.codex/config.toml'), ('claude-code', home / '.claude.json')]:
        if client not in clients:
            continue
        data = tomllib.loads(path.read_text(encoding='utf-8')) if client == 'codex' else json.loads(path.read_text(encoding='utf-8'))
        for name, value in data.get('mcp_servers' if client == 'codex' else 'mcpServers', {}).items():
            if (name.startswith('hoh-') and value.get('url', '').startswith('http://127.0.0.1:')
                    and (servers is None or name in servers)):
                profiles.append((client, name, value))
    if servers is not None:
        for client in clients:
            if set(servers) - {name for actual, name, _ in profiles if actual == client}:
                raise RuntimeError('A selected shared profile is missing from a selected client')
    if not profiles:
        raise RuntimeError('No shared local profiles found')
    checks = []
    async with AsyncExitStack() as stack:
        for index in range(max(len(profiles), connections)):
            client, name, profile = profiles[index % len(profiles)]
            headers = profile_headers(profile)
            http = await stack.enter_async_context(httpx.AsyncClient(headers=headers, timeout=30, trust_env=False))
            read, write, _ = await stack.enter_async_context(streamable_http_client(profile['url'], http_client=http))
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            result = await session.call_tool('session.bootstrap', {})
            if result.isError:
                raise RuntimeError('Bootstrap failed for ' + name)
            identity = result.structuredContent['identity']
            tools = {tool.name for tool in (await session.list_tools()).tools}
            role = identity['role']
            canonical = name == 'hoh-' + identity['agent_id'] and name.startswith('hoh-local-' + role + '-')
            legacy = servers is not None and name in servers and name == 'hoh-' + role
            if not (canonical or legacy) or identity['project_ids'] != [project]:
                raise RuntimeError('Profile identity does not match the expected actor, role and project')
            own_tool = {'developer':'dev.submit_candidate', 'planner':'planner.create_ticket',
                        'qa':'qa.run_project_tests', 'integrator':'integrator.merge_candidate'}[role]
            if own_tool not in tools:
                raise RuntimeError('Expected role tool is missing')
            for other_role, tool in {'developer':'dev.submit_candidate', 'qa':'qa.submit_verdict',
                                     'integrator':'integrator.merge_candidate', 'planner':'planner.create_ticket'}.items():
                if other_role != role:
                    if tool in tools:
                        raise RuntimeError('Unexpected role tool is exposed')
            checks.append({'client':client, 'server_name':name, 'identity':identity})
        print(json.dumps({'status':'connected', 'connection_count':len(checks), 'checks':checks}), flush=True)
        await asyncio.sleep(hold)
    print(json.dumps({'status':'closed_cleanly', 'connection_count':len(checks)}), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path, default=Path.home())
    parser.add_argument('--project', required=True, help='Exact expected project scope')
    parser.add_argument('--server', action='append', help='Exact server name to verify; repeat for multiple profiles')
    parser.add_argument('--client', action='append', choices=['codex', 'claude-code'], help='Selected client; default both')
    parser.add_argument('--connections', type=int, default=7)
    parser.add_argument('--hold-seconds', type=float, default=0)
    arguments = parser.parse_args()
    try:
        asyncio.run(verify(arguments.home, arguments.connections, arguments.hold_seconds, arguments.project,
                           arguments.server, arguments.client))
    except Exception:
        # HTTP libraries may include request data in nested exceptions.
        raise SystemExit('Shared profile verification failed; check credentials and expected scope') from None
