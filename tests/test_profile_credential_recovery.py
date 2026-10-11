"""Synthetic credential/profile recovery; no installed config or user registry."""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import sys
import tomllib
from types import SimpleNamespace

import pytest

from hoh_control_plane import local_agent, local_credentials
from hoh_control_plane.auth import credentials_from_environment, resolve_actor, token_digest


@pytest.fixture
def setup(tmp_path, monkeypatch):
    root, home = tmp_path / 'host', tmp_path / 'home'
    root.mkdir()
    home.mkdir()
    monkeypatch.setattr(local_agent, 'checked_project', lambda *_: {'mcp_port': 8931})
    environment = {}
    monkeypatch.setattr(local_credentials, 'token_delivery_available', lambda: True)
    monkeypatch.setattr(local_credentials, 'deliver_token', environment.__setitem__)
    monkeypatch.setattr(local_credentials, 'read_token', environment.get, raising=False)
    actor = local_agent.profile_actor('shop', 'qa', 'primary')
    def install(client='codex', **kwargs):
        return local_agent.install_profile(root, 'shop', 'qa', 'primary', client, home,
                                           transport='shared-http', **kwargs)
    return root, home, actor, environment, install


def legacy(setup, client, *, wrong_token=False, wrong_url=False, plaintext=False):
    root, home, actor, environment, install = setup
    token = local_credentials.register_local_credential(root, actor)
    if plaintext:
        path = root / 'config/agent-credentials.json'
        records = json.loads(path.read_text())
        records[token] = records.pop(token_digest(token))
        path.write_text(json.dumps(records))
    bearer = 'invalid-synthetic-token-' * 3 if wrong_token else token
    url = 'http://127.0.0.1:9999/mcp' if wrong_url else 'http://127.0.0.1:8931/mcp'
    name = 'hoh-' + actor.agent_id
    if client == 'codex':
        path = home / '.codex/config.toml'
        path.parent.mkdir()
        path.write_text('service_tier = "default"\n# keep this\n' + f'[mcp_servers.{name}]\nurl = "{url}"\nenabled = true\n'
                        + f'[mcp_servers.{name}.http_headers]\nAuthorization = "Bearer {bearer}"\n'
                        + '[mcp_servers.other]\ncommand = "untouched"\n')
    else:
        path = home / '.claude.json'
        path.write_text(json.dumps({'unrelated': 42, 'mcpServers': {name: {
            'type': 'http', 'url': url, 'headers': {'Authorization': 'Bearer ' + bearer}, 'timeout': 90000000}}}))
    return token, path


@pytest.mark.parametrize('client', ['codex', 'claude-code'])
@pytest.mark.parametrize('plaintext', [False, True])
def test_legacy_upgrade_retains_token_identity_settings_and_retries(setup, client, plaintext, capsys):
    root, home, actor, environment, install = setup
    token, path = legacy(setup, client, plaintext=plaintext)
    result = install(client)
    assert result['agent_id'] == actor.agent_id
    assert environment[result['token_env_var']] == token
    credentials = credentials_from_environment((root / 'config/agent-credentials.json').read_text())
    assert resolve_actor(credentials, 'Bearer ' + token) == actor
    assert all(c.hashed for c in credentials)
    first = path.read_text()
    install(client)
    assert path.read_text() == first
    if client == 'codex':
        data = tomllib.loads(first)
        assert data['service_tier'] == 'default' and '# keep this' in first
        assert data['mcp_servers']['other'] == {'command': 'untouched'}
        assert data['mcp_servers'][result['server_name']]['enabled'] is True
    else:
        data = json.loads(first)
        assert data['unrelated'] == 42
        assert data['mcpServers'][result['server_name']]['timeout'] == 90000000
    assert token not in capsys.readouterr().out
    assert all(token not in p.read_text(encoding='utf-8') for p in home.rglob('*') if p.is_file())
    assert token not in (root / 'config/agent-credentials.json').read_text()


@pytest.mark.parametrize('client', ['codex', 'claude-code'])
@pytest.mark.parametrize('bad', ['wrong_token', 'wrong_url'])
def test_unrecognized_http_profile_refused_before_rotation(setup, client, bad):
    root, home, actor, environment, install = setup
    token, path = legacy(setup, client, **{bad: True})
    before = path.read_bytes()
    credential_path = root / 'config/agent-credentials.json'
    credentials_before = credential_path.read_bytes()
    with pytest.raises(ValueError, match='differs'):
        install(client, rotate_token=True)
    assert path.read_bytes() == before
    assert credential_path.read_bytes() == credentials_before
    assert environment == {}


@pytest.mark.parametrize('client', ['codex', 'claude-code'])
def test_unrecognized_http_transport_refused(setup, client):
    root, home, actor, environment, install = setup
    token, path = legacy(setup, client)
    if client == 'codex':
        text = path.read_text().replace('enabled = true', 'enabled = true\ntype = "stdio"')
        path.write_text(text)
    else:
        config = json.loads(path.read_text())
        config['mcpServers']['hoh-' + actor.agent_id]['type'] = 'stdio'
        path.write_text(json.dumps(config))
    before = path.read_bytes()
    with pytest.raises(ValueError, match='differs'):
        install(client)
    assert path.read_bytes() == before and not environment


@pytest.mark.parametrize('rotate', [False, True])
def test_delivery_failure_never_publishes_or_revokes_and_retry_works(setup, monkeypatch, rotate):
    root, home, actor, environment, install = setup
    old = install() if rotate else None
    previous = environment.get(local_credentials.token_env_var(actor.agent_id))
    def broken(*_):
        raise OSError('synthetic-sensitive-provider-message')
    monkeypatch.setattr(local_credentials, 'deliver_token', broken)
    with pytest.raises(RuntimeError, match='incomplete') as error:
        install(rotate_token=rotate)
    assert 'synthetic-sensitive-provider-message' not in str(error.value)
    path = root / 'config/agent-credentials.json'
    if previous:
        assert resolve_actor(credentials_from_environment(path.read_text()), 'Bearer ' + previous) == actor
    else:
        assert not path.exists()
    monkeypatch.setattr(local_credentials, 'deliver_token', environment.__setitem__)
    result = install(rotate_token=rotate)
    assert resolve_actor(credentials_from_environment(path.read_text()), 'Bearer ' + environment[result['token_env_var']]) == actor


def test_late_config_failure_restores_delivered_old_token(setup, monkeypatch):
    from hoh_control_plane.agent_assets import install as installer
    root, home, actor, environment, install = setup
    result = install()
    old = environment[result['token_env_var']]
    actual = installer.install
    def fail(kit, home, client, *, dry_run=False):
        if dry_run:
            return actual(kit, home, client, dry_run=True)
        raise OSError('synthetic-config-failure')
    monkeypatch.setattr(installer, 'install', fail)
    with pytest.raises(RuntimeError, match='incomplete'):
        install(rotate_token=True)
    assert environment[result['token_env_var']] == old
    credentials = credentials_from_environment((root / 'config/agent-credentials.json').read_text())
    assert resolve_actor(credentials, 'Bearer ' + old) == actor


def test_missing_delivered_credential_requires_explicit_recovery(setup):
    root, home, actor, environment, install = setup
    install()
    environment.clear()
    with pytest.raises(RuntimeError, match='recovery needed'):
        install()
    install(rotate_token=True)
    assert environment


def test_publication_failure_restores_old_token_and_retry_authenticates(setup, monkeypatch):
    root, home, actor, environment, install = setup
    result = install()
    old = environment[result['token_env_var']]
    path = root / 'config/agent-credentials.json'
    before = path.read_bytes()
    provider = local_credentials.credential_provider((), path)
    assert resolve_actor(provider(), 'Bearer ' + old) == actor
    actual = os.replace
    def fail_publication(source, destination):
        if Path(destination) == path:
            raise OSError('synthetic-publication-failure')
        return actual(source, destination)
    monkeypatch.setattr(os, 'replace', fail_publication)
    with pytest.raises(RuntimeError, match='incomplete'):
        install(rotate_token=True)
    assert path.read_bytes() == before
    assert environment[result['token_env_var']] == old
    assert not list(path.parent.glob('.credentials-*'))
    monkeypatch.setattr(os, 'replace', actual)
    install(rotate_token=True)
    credentials = credentials_from_environment(path.read_text())
    assert resolve_actor(credentials, 'Bearer ' + environment[result['token_env_var']]) == actor
    assert environment[result['token_env_var']] != old
    from hoh_control_plane.domain import AuthorizationError
    assert resolve_actor(provider(), 'Bearer ' + environment[result['token_env_var']]) == actor
    with pytest.raises(AuthorizationError):
        resolve_actor(provider(), 'Bearer ' + old)


def test_registry_read_failure_is_redacted_and_preserves_credential(setup, monkeypatch):
    root, home, actor, environment, install = setup
    result = install()
    previous = environment[result['token_env_var']]
    path = root / 'config/agent-credentials.json'
    before = path.read_bytes()
    def broken_read(_):
        raise OSError(previous)
    monkeypatch.setattr(local_credentials, 'read_token', broken_read)
    with pytest.raises(RuntimeError, match='recovery needed') as error:
        install(rotate_token=True)
    assert previous not in str(error.value)
    assert path.read_bytes() == before
    assert environment[result['token_env_var']] == previous


@pytest.mark.skipif(os.name != 'nt', reason='Windows registry boundary')
def test_read_token_uses_fake_persistent_registry_over_parent_environment(monkeypatch):
    from contextlib import nullcontext
    persistent = 'synthetic-persistent-token'
    monkeypatch.setenv('SYNTHETIC_TOKEN', 'synthetic-stale-parent-token')
    monkeypatch.setitem(sys.modules, 'winreg', SimpleNamespace(
        HKEY_CURRENT_USER=object(), REG_SZ=1,
        OpenKey=lambda *_: nullcontext(object()),
        QueryValueEx=lambda *_: (persistent, 1)))
    assert local_credentials.read_token('SYNTHETIC_TOKEN') == persistent


def verifier():
    spec = importlib.util.spec_from_file_location('verify_profiles', Path(__file__).parents[1] / 'deploy/desktop/verify_shared_profiles.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('client', ['codex', 'claude-code'])
@pytest.mark.parametrize('alias', [None, 'hoh-qa'])
def test_bulk_http_migration_preserves_actor_and_never_backs_up_literal(setup, monkeypatch, client, alias):
    from hoh_control_plane import profile_migration
    root, home, actor, environment, install = setup
    token, path = legacy(setup, client)
    if alias:
        path.write_text(path.read_text().replace('hoh-' + actor.agent_id, alias))
    (root / 'config/settings.json').write_text(json.dumps({'mcp_port': 8931}))
    monkeypatch.setattr(profile_migration, 'checked_project', lambda *_: {})
    monkeypatch.setattr(profile_migration, 'deliver_token', environment.__setitem__)
    monkeypatch.setattr(profile_migration, 'token_delivery_available', lambda: True)
    before = path.read_bytes()
    preview = profile_migration.migrate(root, home, client)
    assert preview['profiles'][0]['agent_id'] == actor.agent_id
    assert path.read_bytes() == before and not environment
    result = profile_migration.migrate(root, home, client, apply=True)
    assert result['backup_path'] is None
    assert environment[local_credentials.token_env_var(actor.agent_id)] == token
    assert alias is None or alias in path.read_text()
    assert not profile_migration.migrate(root, home, client, apply=True)['profiles']
    assert all(token not in p.read_text(encoding='utf-8') for p in home.rglob('*') if p.is_file())


@pytest.mark.parametrize('bad', ['wrong_url', 'wrong_alias_role', 'arbitrary_alias'])
def test_bulk_migration_conflict_preflights_all_profiles_before_delivery(setup, monkeypatch, bad):
    from hoh_control_plane import profile_migration
    root, home, actor, environment, install = setup
    token, path = legacy(setup, 'claude-code')
    (root / 'config/settings.json').write_text(json.dumps({'mcp_port': 8931}))
    monkeypatch.setattr(profile_migration, 'checked_project', lambda *_: {})
    monkeypatch.setattr(profile_migration, 'deliver_token', environment.__setitem__)
    monkeypatch.setattr(profile_migration, 'token_delivery_available', lambda: True)
    config = json.loads(path.read_text())
    name = 'hoh-developer' if bad == 'wrong_alias_role' else 'hoh-unrecognized'
    url = 'http://127.0.0.1:9999/mcp' if bad == 'wrong_url' else 'http://127.0.0.1:8931/mcp'
    config['mcpServers'][name] = {'type': 'http', 'url': url, 'headers': {'Authorization': 'Bearer ' + token}}
    path.write_text(json.dumps(config))
    before = path.read_bytes()
    with pytest.raises(ValueError, match='Unrecognized'):
        profile_migration.migrate(root, home, 'claude-code', apply=True)
    assert path.read_bytes() == before and not environment


@pytest.mark.parametrize('bad', ['wrong_variable', 'missing_token', 'wrong_url', 'wrong_type'])
def test_converted_reference_requires_exact_valid_recovery(setup, monkeypatch, bad):
    from hoh_control_plane import profile_migration
    root, home, actor, environment, install = setup
    token, path = legacy(setup, 'claude-code')
    (root / 'config/settings.json').write_text(json.dumps({'mcp_port': 8931}))
    monkeypatch.setattr(profile_migration, 'checked_project', lambda *_: {})
    monkeypatch.setattr(profile_migration, 'deliver_token', environment.__setitem__)
    monkeypatch.setattr(profile_migration, 'token_delivery_available', lambda: True)
    profile_migration.migrate(root, home, 'claude-code', apply=True)
    config = json.loads(path.read_text())
    profile = config['mcpServers']['hoh-' + actor.agent_id]
    if bad == 'wrong_variable':
        profile['headers']['Authorization'] = 'Bearer ${SYNTHETIC_WRONG_VARIABLE}'
        environment['SYNTHETIC_WRONG_VARIABLE'] = token
    elif bad == 'missing_token':
        environment.clear()
    elif bad == 'wrong_url':
        profile['url'] = 'http://127.0.0.1:9999/mcp'
    else:
        profile['type'] = 'stdio'
    path.write_text(json.dumps(config))
    before = path.read_bytes()
    with pytest.raises((ValueError, RuntimeError)):
        profile_migration.migrate(root, home, 'claude-code', apply=True)
    assert path.read_bytes() == before


def test_bulk_variable_collision_refuses_before_delivery(setup, monkeypatch):
    from hoh_control_plane import profile_migration
    from hoh_control_plane.domain import Actor, Role
    root, home, actor, environment, install = setup
    token, path = legacy(setup, 'claude-code')
    (root / 'config/settings.json').write_text(json.dumps({'mcp_port': 8931}))
    monkeypatch.setattr(profile_migration, 'checked_project', lambda *_: {})
    monkeypatch.setattr(profile_migration, 'deliver_token', environment.__setitem__)
    monkeypatch.setattr(profile_migration, 'token_delivery_available', lambda: True)
    for name in ['synthetic-a', 'synthetic_a']:
        local_credentials.register_local_credential(root, Actor(name, Role.QA, frozenset({'shop'})))
    before = path.read_bytes()
    with pytest.raises(ValueError, match='collision'):
        profile_migration.migrate(root, home, 'claude-code', apply=True)
    assert path.read_bytes() == before and not environment


def test_migration_rechecks_config_after_delivery_without_nested_lock(setup, monkeypatch):
    from contextlib import contextmanager
    from hoh_control_plane import profile_migration
    from hoh_control_plane.agent_assets import install as installer
    root, home, actor, environment, install = setup
    token, path = legacy(setup, 'claude-code')
    (root / 'config/settings.json').write_text(json.dumps({'mcp_port': 8931}))
    monkeypatch.setattr(profile_migration, 'checked_project', lambda *_: {})
    monkeypatch.setattr(profile_migration, 'token_delivery_available', lambda: True)
    locked = False
    @contextmanager
    def config_lock(_):
        nonlocal locked
        assert not locked
        locked = True
        try:
            yield
        finally:
            locked = False
    changed = json.loads(path.read_text())
    changed['unrelated'] = 'concurrent-client-update'
    def delivery(name, value):
        assert not locked, 'credential operation must not nest under config lock'
        environment[name] = value
        path.write_text(json.dumps(changed))
    monkeypatch.setattr(installer, '_config_lock', config_lock)
    monkeypatch.setattr(profile_migration, 'deliver_token', delivery)
    with pytest.raises(RuntimeError, match='changed before write'):
        profile_migration.migrate(root, home, 'claude-code', apply=True)
    assert json.loads(path.read_text()) == changed
    assert resolve_actor(credentials_from_environment((root / 'config/agent-credentials.json').read_text()),
                         'Bearer ' + token) == actor


@pytest.mark.parametrize('client', ['codex', 'claude-code'])
def test_verifier_resolves_references_and_missing_variable_is_redacted(monkeypatch, client):
    module = verifier()
    token = 'synthetic-verifier-token-' * 3
    monkeypatch.setenv('SYNTHETIC_TOKEN', token)
    profile = {'bearer_token_env_var': 'SYNTHETIC_TOKEN'} if client == 'codex' else {'headers': {'Authorization': 'Bearer ${SYNTHETIC_TOKEN}'}}
    assert module.profile_headers(profile) == {'Authorization': 'Bearer ' + token}
    monkeypatch.delenv('SYNTHETIC_TOKEN')
    with pytest.raises(RuntimeError, match='missing or invalid') as error:
        module.profile_headers(profile)
    assert token not in str(error.value)

@pytest.mark.parametrize('case', ['valid', 'wrong_token', 'wrong_project', 'wrong_role', 'legacy_alias', 'wrong_alias_role',
                                 'missing_tool', 'wrong_tools'])
def test_verifier_authenticates_fixture_http_and_exact_scope(tmp_path, monkeypatch, capsys, case):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread
    module = verifier()
    token = 'synthetic-http-bearer-' * 3
    actor = local_agent.profile_actor('shop', 'qa', 'primary')
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def do_POST(self):
            if self.headers.get('Authorization') != 'Bearer ' + token:
                self.send_response(401)
                self.end_headers()
                return
            request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            if 'id' not in request:
                self.send_response(202)
                self.end_headers()
                return
            if request['method'] == 'initialize':
                result = {'protocolVersion': '2025-03-26', 'capabilities': {'tools': {}}, 'serverInfo': {'name': 'fixture', 'version': '1'}}
            elif request['method'] == 'tools/list':
                names = [] if case == 'missing_tool' else ['qa.run_project_tests']
                if case == 'wrong_tools':
                    names.append('dev.submit_candidate')
                result = {'tools': [{'name': name, 'inputSchema': {'type': 'object'}} for name in names]}
            else:
                identity = {'agent_id': actor.agent_id, 'role': 'developer' if case in {'wrong_role', 'wrong_alias_role'} else 'qa',
                            'project_ids': ['other' if case == 'wrong_project' else 'shop']}
                result = {'content': [], 'structuredContent': {'identity': identity}, 'isError': False}
            body = json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': result}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def do_GET(self):
            self.send_response(405)
            self.end_headers()
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv('SYNTHETIC_TOKEN', token if case != 'wrong_token' else 'synthetic-incorrect-token-' * 3)
    url = f'http://127.0.0.1:{server.server_port}/mcp'
    (tmp_path / '.codex').mkdir()
    (tmp_path / '.codex/config.toml').write_text(f'[mcp_servers.hoh-{actor.agent_id}]\nurl = "{url}"\nbearer_token_env_var = "SYNTHETIC_TOKEN"\n')
    (tmp_path / '.claude.json').write_text(json.dumps({'mcpServers': {'hoh-' + actor.agent_id: {
        'type': 'http', 'url': url, 'headers': {'Authorization': 'Bearer ${SYNTHETIC_TOKEN}'}}}}))
    if case in {'legacy_alias', 'wrong_alias_role'}:
        for path in [tmp_path / '.codex/config.toml', tmp_path / '.claude.json']:
            path.write_text(path.read_text().replace('hoh-' + actor.agent_id, 'hoh-qa'))
    try:
        if case in {'valid', 'legacy_alias'}:
            config = json.loads((tmp_path / '.claude.json').read_text())
            config['mcpServers']['hoh-local-qa-unrelated'] = {
                'type': 'http', 'url': url, 'headers': {'Authorization': 'Bearer ${ABSENT_UNRELATED_TOKEN}'}}
            (tmp_path / '.claude.json').write_text(json.dumps(config))
            selected = 'hoh-qa' if case == 'legacy_alias' else 'hoh-' + actor.agent_id
            asyncio.run(module.verify(tmp_path, 2, 0, 'shop', [selected]))
            assert 'closed_cleanly' in capsys.readouterr().out
            with pytest.raises(RuntimeError, match='missing'):
                asyncio.run(module.verify(tmp_path, 2, 0, 'shop', ['hoh-missing']))
            config['mcpServers'].pop(selected)
            (tmp_path / '.claude.json').write_text(json.dumps(config))
            with pytest.raises(RuntimeError, match='missing'):
                asyncio.run(module.verify(tmp_path, 2, 0, 'shop', [selected]))
            asyncio.run(module.verify(tmp_path, 1, 0, 'shop', [selected], ['codex']))
        else:
            with pytest.raises(Exception):
                asyncio.run(module.verify(tmp_path, 2, 0, 'shop', ['hoh-qa']) if case == 'wrong_alias_role'
                            else module.verify(tmp_path, 2, 0, 'shop'))
        captured = capsys.readouterr()
        assert token not in captured.out + captured.err
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
