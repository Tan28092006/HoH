"""Read-only bootstrap verification and optional concurrent-client resource probe."""
import argparse
import asyncio
from contextlib import AsyncExitStack
import json
from pathlib import Path
import tomllib

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def verify(home, connections, hold):
    profiles = []
    for client, path in [('codex', home / '.codex/config.toml'), ('claude-code', home / '.claude.json')]:
        data = tomllib.loads(path.read_text(encoding='utf-8')) if client == 'codex' else json.loads(path.read_text(encoding='utf-8'))
        for name, value in data.get('mcp_servers' if client == 'codex' else 'mcpServers', {}).items():
            if name.startswith('hoh-') and value.get('url', '').startswith('http://127.0.0.1:'):
                profiles.append((client, name, value))
    if not profiles:
        raise RuntimeError('No shared local profiles found')
    checks = []
    async with AsyncExitStack() as stack:
        for index in range(max(len(profiles), connections)):
            client, name, profile = profiles[index % len(profiles)]
            headers = profile.get('http_headers', profile.get('headers', {}))
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
            own_tool = {'developer':'dev.submit_candidate', 'planner':'planner.create_ticket',
                        'qa':'qa.run_project_tests', 'integrator':'integrator.merge_candidate'}[role]
            assert own_tool in tools
            for other_role, tool in {'developer':'dev.submit_candidate', 'qa':'qa.submit_verdict',
                                     'integrator':'integrator.merge_candidate', 'planner':'planner.create_ticket'}.items():
                if other_role != role:
                    assert tool not in tools
            checks.append({'client':client, 'server_name':name, 'identity':identity})
        print(json.dumps({'status':'connected', 'connection_count':len(checks), 'checks':checks}), flush=True)
        await asyncio.sleep(hold)
    print(json.dumps({'status':'closed_cleanly', 'connection_count':len(checks)}), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path, default=Path.home())
    parser.add_argument('--connections', type=int, default=7)
    parser.add_argument('--hold-seconds', type=float, default=0)
    arguments = parser.parse_args()
    asyncio.run(verify(arguments.home, arguments.connections, arguments.hold_seconds))
