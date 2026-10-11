import json
import hashlib
from contextlib import closing
from pathlib import Path
import subprocess
import sys
import tomllib
import zipfile
import pytest
from fastapi.testclient import TestClient


from helpers.desktop import desktop, headers  # noqa: F401  (fixture)


def test_create_project_lists_and_selects_exact_id(desktop):
    client, root = desktop
    response = client.post('/local/projects', json={'project_id': 'shop', 'name': 'My shop', 'default_branch': 'main'}, headers=headers(client))
    assert response.status_code == 201
    assert response.json()['project_id'] == 'shop'
    session = client.get('/local/session').json()
    assert [p['project_id'] for p in session['projects']] == ['shop']
    assert client.post('/local/projects', json={'project_id': 'shop', 'name': 'Duplicate'}, headers=headers(client)).status_code == 409
    assert client.post('/local/projects', json={'project_id': '../escape', 'name': 'Bad'}, headers=headers(client)).status_code == 422
    prompts = client.get('/local/prompts?project_id=shop').json()['prompts']
    assert set(prompts) == {'planner', 'developer', 'qa', 'integrator'}
    assert all('shop' in prompt and 'session.bootstrap' in prompt for prompt in prompts.values())
    assert client.get('/local/prompts?project_id=missing').status_code == 404


@pytest.mark.parametrize('bad_headers', [{}, {'Origin': 'https://evil.example'},
    {'Origin': 'http://127.0.0.1:8765', 'X-HoH-Local-Token': 'wrong'}])
def test_external_or_unproved_writes_are_refused(desktop, bad_headers):
    client, root = desktop
    assert client.post('/local/shutdown', headers=bad_headers).status_code == 403
    assert not (root / 'config/shutdown.request').exists()
    assert client.post('/api/v1/projects', json={}).status_code == 405
    assert client.get('/local/session', headers={'Host': 'evil.example'}).status_code == 403


def test_shutdown_pauses_admission_and_requests_drain(desktop):
    client, root = desktop
    response = client.post('/local/shutdown', headers=headers(client))
    assert response.status_code == 202
    assert json.loads((root / 'config/shutdown.request').read_text())['resume_on_start'] is True
    assert client.get('/local/session').json()['shutting_down'] is True
    assert client.post('/local/projects', json={'project_id': 'late', 'name': 'Late'}, headers=headers(client)).status_code == 409
    assert client.post('/local/shutdown', headers=headers(client)).status_code == 202
    assert json.loads((root / 'config/shutdown.request').read_text())['resume_on_start'] is True


def test_project_bound_agent_installs_both_clients_and_preserves_other_settings(desktop, tmp_path):
    from hoh_control_plane.local_agent import install_profile, profile_actor
    client, root = desktop
    client.post('/local/projects', json={'project_id': 'shop', 'name': 'Shop'}, headers=headers(client))
    for client_name in ('codex', 'claude-code'):
        home = tmp_path / client_name
        (home / '.codex').mkdir(parents=True)
        (home / '.codex/config.toml').write_text('model = "existing-model"\n')
        (home / '.claude.json').write_text('{"unrelated":42}')
        for role in ('planner', 'developer', 'qa', 'integrator'):
            result = install_profile(root, 'shop', role, 'primary', client_name, home)
            repeated = install_profile(root, 'shop', role, 'primary', client_name, home)
            assert result['server_name'] == repeated['server_name']
            actor = profile_actor('shop', role, 'primary')
            assert actor.project_ids == frozenset({'shop'}) and actor.role.value == role
        assert 'existing-model' in (home / '.codex/config.toml').read_text()
        assert json.loads((home / '.claude.json').read_text())['unrelated'] == 42
    assert profile_actor('shop', 'developer', 'second').agent_id != profile_actor('shop', 'developer', 'primary').agent_id
    with pytest.raises(Exception):
        install_profile(root, 'missing', 'planner', 'primary', 'codex', tmp_path / 'other')


def test_drain_waits_for_workers_and_reopen_restores_admission(desktop):
    import sqlite3
    from hoh_control_plane.desktop_control import shutdown_ready, prepare_desktop_start
    client, root = desktop
    client.post('/local/shutdown', headers=headers(client))
    assert not shutdown_ready(root)  # A quiet period allows an in-flight tick to publish.
    marker = root / 'config/shutdown.request'
    content = json.loads(marker.read_text())
    content['requested_at'] = 0
    marker.write_text(json.dumps(content))
    database = json.loads((root / 'config/settings.json').read_text())['database_path']
    with closing(sqlite3.connect(database)) as db, db:
        db.execute("INSERT INTO worker_executions (execution_id, runner_id, job_kind, job_id, status, started_at, heartbeat_at) VALUES ('x','test','qa','test-job','RUNNING','now','now')")
    assert not shutdown_ready(root)
    with closing(sqlite3.connect(database)) as db, db:
        db.execute("UPDATE worker_executions SET status='FINISHED' WHERE execution_id='x'")
    assert shutdown_ready(root)
    prepare_desktop_start(root)
    assert not marker.exists()
    with closing(sqlite3.connect(database)) as db:
        assert db.execute("SELECT state_value FROM runtime_state WHERE state_key='paused'").fetchone()[0] == '0'


@pytest.mark.parametrize('client_name', ['codex', 'claude-code'])
def test_shared_http_upgrades_only_generated_profile_and_keeps_identity(desktop, tmp_path, client_name,
                                                                      delivered_tokens, monkeypatch):
    monkeypatch.setattr("hoh_control_plane.local_credentials.read_token", delivered_tokens.get)
    import tomllib
    from hoh_control_plane.auth import credentials_from_environment
    from hoh_control_plane.local_agent import install_profile
    client, root = desktop
    client.post('/local/projects', json={'project_id': 'shop', 'name': 'Shop'}, headers=headers(client))
    home = tmp_path / 'client'
    old = install_profile(root, 'shop', 'qa', 'primary', client_name, home)
    new = install_profile(root, 'shop', 'qa', 'primary', client_name, home, transport='shared-http')
    assert old['agent_id'] == new['agent_id'] and old['server_name'] == new['server_name']
    path = home / ('.codex/config.toml' if client_name == 'codex' else '.claude.json')
    content = path.read_text()
    entries = tomllib.loads(content)['mcp_servers'] if client_name == 'codex' else json.loads(content)['mcpServers']
    profile = entries[new['server_name']]
    assert 'command' not in profile and 'args' not in profile
    assert profile['url'].startswith('http://127.0.0.1:')
    credentials = credentials_from_environment((root / 'config/agent-credentials.json').read_text())
    assert len(credentials) == 1
    assert credentials[0].actor.agent_id == new['agent_id']
    assert credentials[0].actor.project_ids == frozenset({'shop'})
    assert credentials[0].token not in json.dumps(new)
    # Wave 8A, R4: the config names an environment variable; the token went to
    # that variable once, and the credential file keeps only its digest.
    from hoh_control_plane.auth import resolve_actor
    variable = new['token_env_var']
    assert new['token_issued'] is True and variable == 'HOH_MCP_TOKEN_' + new['agent_id'].upper().replace('-', '_')
    token = delivered_tokens[variable]
    assert token not in content and 'Bearer ' + token not in content
    assert token not in (root / 'config/agent-credentials.json').read_text()
    assert credentials[0].hashed
    if client_name == 'codex':
        assert profile['bearer_token_env_var'] == variable and 'http_headers' not in profile
    else:
        assert profile['headers'] == {'Authorization': 'Bearer ${' + variable + '}'}
    assert resolve_actor(credentials, 'Bearer ' + token).agent_id == new['agent_id']
    repeated = install_profile(root, 'shop', 'qa', 'primary', client_name, home, transport='shared-http')
    assert repeated['token_issued'] is False and delivered_tokens[variable] == token
    assert path.read_text() == content


def test_legacy_migration_preserves_actor_and_unrelated_config(desktop, tmp_path, delivered_tokens, monkeypatch):
    monkeypatch.setattr("hoh_control_plane.local_credentials.read_token", delivered_tokens.get)
    import tomllib
    from hoh_control_plane.profile_migration import migrate
    from hoh_control_plane.auth import credentials_from_environment
    client, root = desktop
    client.post('/local/projects', json={'project_id':'shop', 'name':'Shop'}, headers=headers(client))
    home = tmp_path / 'client'
    path = home / '.codex/config.toml'
    path.parent.mkdir(parents=True)
    legacy = tmp_path / 'legacy'
    args = ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(legacy / 'scripts/start-mcp.ps1')]
    untouched = '\n# preserve comment\n[mcp_servers.other]\ncommand = "untouched"\n\n[projects.example]\ntrust_level = "trusted"\n'
    original = ('model = "keep"\n[mcp_servers.hoh-developer]\ncommand = "powershell.exe"\nargs = '
                + json.dumps(args) + '\n[mcp_servers.hoh-developer.env]\nHOH_AGENT_ID = "original-dev"\n'
                'HOH_AGENT_ROLE = "developer"\nHOH_PROJECT_IDS = "shop"\n' + untouched)
    path.write_text(original)
    preview = migrate(root, home, 'codex', legacy)
    assert len(preview['profiles']) == 1 and path.read_text() == original
    result = migrate(root, home, 'codex', legacy, apply=True)
    assert result['backup_path'] is None
    assert not list(path.parent.glob(path.name + '.hoh-http-backup-*'))
    parsed = tomllib.loads(path.read_text())
    assert parsed['model'] == 'keep' and parsed['mcp_servers']['other']['command'] == 'untouched'
    assert parsed['projects']['example']['trust_level'] == 'trusted'
    assert '# preserve comment' in path.read_text()
    assert 'command' not in parsed['mcp_servers']['hoh-developer']
    credentials = credentials_from_environment((root / 'config/agent-credentials.json').read_text())
    assert credentials[0].actor.agent_id == 'original-dev'
    # Wave 8A, R4: no literal bearer token in the migrated profile.
    assert parsed['mcp_servers']['hoh-developer']['bearer_token_env_var'] == 'HOH_MCP_TOKEN_ORIGINAL_DEV'
    assert 'http_headers' not in parsed['mcp_servers']['hoh-developer'] and 'Bearer' not in path.read_text()
    assert credentials[0].hashed
    assert not migrate(root, home, 'codex', legacy, apply=True)['profiles']


def test_explicit_skill_upgrade_keeps_backup_and_rolls_back_config_conflict(desktop, tmp_path):
    from hoh_control_plane.local_agent import install_profile
    client, root = desktop
    client.post('/local/projects', json={'project_id': 'shop', 'name': 'Shop'}, headers=headers(client))
    home = tmp_path / 'upgrade-client'
    result = install_profile(root, 'shop', 'planner', 'primary', 'codex', home)
    skill = home / '.codex/skills/hoh-planner/SKILL.md'
    skill.write_text('old user customization')
    with pytest.raises(ValueError):
        install_profile(root, 'shop', 'planner', 'primary', 'codex', home)
    upgraded = install_profile(root, 'shop', 'planner', 'primary', 'codex', home, upgrade_skills=True)
    assert len(upgraded['skill_backups']) == 1
    assert (Path(upgraded['skill_backups'][0]) / 'SKILL.md').read_text() == 'old user customization'
    config = home / '.codex/config.toml'
    config.write_text(config.read_text().replace('hoh_control_plane.local_agent', 'conflicting_module'))
    skill.write_text('preserve me on failure')
    with pytest.raises(ValueError):
        install_profile(root, 'shop', 'planner', 'primary', 'codex', home, upgrade_skills=True)
    assert skill.read_text() == 'preserve me on failure'


def test_stdio_profile_authenticates_exact_role_and_rejects_other_project(desktop):
    import asyncio
    import os
    import sys
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    client, root = desktop
    client.post('/local/projects', json={'project_id': 'shop', 'name': 'Shop'}, headers=headers(client))
    client.post('/local/projects', json={'project_id': 'other', 'name': 'Other'}, headers=headers(client))
    (root / 'hoh_desktop.py').write_text('pass\n')
    async def check():
        parameters = StdioServerParameters(command=sys.executable,
            args=['-m', 'hoh_control_plane.local_agent', 'serve', '--root', str(root), '--project', 'shop', '--role', 'planner'],
            env={**os.environ, 'HOH_AGENT_ROLE': 'admin', 'HOH_PROJECT_IDS': '*'})
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool('session.bootstrap', {'project_id': 'shop'})
                assert not result.isError
                assert result.structuredContent['identity']['role'] == 'planner'
                assert result.structuredContent['identity']['project_ids'] == ['shop']
                assert result.structuredContent['selected_project_id'] == 'shop'
                denied = await session.call_tool('session.bootstrap', {'project_id': 'other'})
                assert denied.isError
    asyncio.run(check())


def test_agent_reconnect_does_not_undo_operator_shutdown(desktop):
    import os
    import subprocess
    import sys
    client, root = desktop
    client.post('/local/projects', json={'project_id': 'shop', 'name': 'Shop'}, headers=headers(client))
    marker = root / 'unexpected-start'
    (root / 'hoh_desktop.py').write_text(f'from pathlib import Path\nPath({str(marker)!r}).touch()\n')
    (root / 'config/stop.request').touch()
    result = subprocess.run([sys.executable, '-m', 'hoh_control_plane.local_agent', 'serve', '--root', str(root),
        '--project', 'shop', '--role', 'planner'], env=os.environ.copy(), input='', capture_output=True, text=True, timeout=20)
    assert not marker.exists(), 'A reconnect must not silently restart an operator-stopped host'
    assert result.returncode != 0


def test_local_reads_close_database_handles(desktop, monkeypatch):
    import sqlite3
    from hoh_control_plane.local_agent import checked_project
    from hoh_control_plane.desktop_control import shutdown_ready
    client, root = desktop
    client.post('/local/projects', json={'project_id': 'shop', 'name': 'Shop'}, headers=headers(client))
    (root / 'config/shutdown.request').write_text('{"requested_at":0,"resume_on_start":true}')
    connections = []
    connect = sqlite3.connect
    def observed_connect(*args, **kwargs):
        connection = connect(*args, **kwargs)
        connections.append(connection)
        return connection
    monkeypatch.setattr(sqlite3, 'connect', observed_connect)
    checked_project(root, 'shop')
    assert shutdown_ready(root)
    try:
        for connection in connections:
            with pytest.raises(sqlite3.ProgrammingError):
                connection.execute('SELECT 1')
    finally:
        for connection in connections:
            connection.close()


def test_gui_wait_profiles_have_long_timeout_and_unique_instances(desktop, tmp_path):
    import tomllib
    from hoh_control_plane.local_agent import install_profile, make_prompts
    client, root = desktop
    client.post('/local/projects', json={'project_id': 'shop', 'name': 'Shop'}, headers=headers(client))
    home = tmp_path / 'gui-client'
    result = install_profile(root, 'shop', 'developer', 'codex-1', 'codex', home)
    config = home / '.codex/config.toml'
    assert tomllib.loads(config.read_text())['mcp_servers'][result['server_name']]['tool_timeout_sec'] == 86400
    # Upgrading an otherwise identical 0.3.1 profile preserves unrelated config.
    config.write_text('model = "configured-model"\n' + config.read_text().replace('tool_timeout_sec = 86400\n', ''))
    install_profile(root, 'shop', 'developer', 'codex-1', 'codex', home)
    assert tomllib.loads(config.read_text())['model'] == 'configured-model'
    assert tomllib.loads(config.read_text())['mcp_servers'][result['server_name']]['tool_timeout_sec'] == 86400
    prompt = make_prompts(root, 'shop', instance='codex-1')['developer']
    assert 'work.wait_for_event' in prompt
    assert 'resume_wait_id' in prompt
    assert '--instance codex-1' in prompt
    assert result['server_name'] in prompt
    expected_python = (root / '.venv' / 'Scripts' / 'python.exe').as_posix()
    assert f"& '{expected_python}' -I" in prompt
    assert f"--root '{root.as_posix()}'" in prompt
    assert 'fully reopen Codex Desktop' in prompt
    planner_prompt = make_prompts(root, 'shop', instance='codex-1')['planner']
    assert 'sync repository' in planner_prompt
    integrator_prompt = make_prompts(root, 'shop', instance='codex-1')['integrator']
    assert 'mainline SHA' in integrator_prompt
    assert client.get('/local/agents?project_id=shop').json() == {'agents': []}


def test_the_setup_command_survives_a_markdown_client(desktop, tmp_path):
    """The command reaches PowerShell as written, or setup cannot even start.

    A Planner setup was blocked because the command sat in the prompt as ordinary
    list text, so a client read `hoh_control_plane` as two italic markers and
    escaped them. PowerShell received `hoh\\_control\\_plane` and reported
    ModuleNotFoundError, and the prompt's own "do not repeat setup" rule then
    stopped the chat rather than letting it retype one command.
    """
    from hoh_control_plane.local_agent import make_prompts
    client, root = desktop
    client.post('/local/projects', json={'project_id': 'shop', 'name': 'Shop'}, headers=headers(client))

    for role, prompt in make_prompts(root, 'shop', instance='codex-1').items():
        command = next(
            line for line in prompt.splitlines() if ' -I -m ' in line
        )
        # A fenced block is copied verbatim; paragraph text is not.
        fences = [
            index for index, line in enumerate(prompt.splitlines())
            if line.strip().startswith('```')
        ]
        command_line = prompt.splitlines().index(command)
        assert len(fences) == 2, role
        assert fences[0] < command_line < fences[1], f"{role}: command is outside the fence"
        assert 'hoh_control_plane.local_agent setup' in command, role
        # No backslash anywhere: not in the interpreter path, not in --root, and
        # nothing a client could mistake for an escape it should preserve.
        assert '\\' not in prompt, role
        # And if a client escapes it anyway, the prompt says how to clear it
        # without treating a retype as a second setup.
        assert 'ModuleNotFoundError' in prompt, role
        assert 'not a repeat of setup' in prompt, role


def test_desktop_host_launcher_escapes_the_calling_process_job():
    """Restarting Codex must not tear down a HoH host launched during setup."""
    launcher = (
        Path(__file__).resolve().parents[1] / 'deploy' / 'desktop' / 'start-host.ps1'
    ).read_text(encoding='utf-8')
    assert 'Invoke-CimMethod' in launcher
    assert 'Win32_Process' in launcher
    assert '-WindowStyle Hidden' in launcher
    assert 'OPENBLAS_NUM_THREADS' in launcher
    assert 'OMP_NUM_THREADS' in launcher
    upgrader = (
        Path(__file__).resolve().parents[1] / 'deploy' / 'desktop' / 'upgrade_local.py'
    ).read_text(encoding='utf-8')
    assert "bundle / 'start-host.ps1'" in upgrader
    assert "backup / 'start-host.ps1'" in upgrader


def test_local_upgrade_packager_includes_checksummed_detached_launcher(tmp_path):
    source = Path(__file__).resolve().parents[1]
    version = tomllib.loads((source / 'pyproject.toml').read_text())['project']['version']
    wheel = tmp_path / f'hoh_control_plane-{version}-py3-none-any.whl'
    wheel.write_bytes(b'test wheel placeholder')
    archive = tmp_path / 'upgrade.zip'

    subprocess.run(
        [
            sys.executable,
            str(source / 'deploy/desktop/package_local.py'),
            '--wheel', str(wheel),
            '--output', str(archive),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    with zipfile.ZipFile(archive) as bundle:
        names = set(bundle.namelist())
        manifest = json.loads(bundle.read('manifest.json'))
        launcher = bundle.read('start-host.ps1')
    assert 'start-host.ps1' in names
    assert manifest['files']['start-host.ps1'] == hashlib.sha256(launcher).hexdigest()


def test_claude_profile_upgrade_handles_idle_timeout_and_preserves_higher_override(desktop, tmp_path):
    from hoh_control_plane.local_agent import install_profile
    client, root = desktop
    client.post('/local/projects', json={'project_id':'shop','name':'Shop'}, headers=headers(client))
    home = tmp_path / 'claude-home'
    result = install_profile(root, 'shop', 'qa', 'primary', 'claude-code', home)
    path = home / '.claude.json'
    data = json.loads(path.read_text())
    name = result['server_name']
    assert data['mcpServers'][name]['timeout'] == 86400000
    data['mcpServers'][name].pop('timeout')
    data['unrelated'] = {'keep':True}
    path.write_text(json.dumps(data))
    install_profile(root, 'shop', 'qa', 'primary', 'claude-code', home)
    upgraded = json.loads(path.read_text())
    assert upgraded['mcpServers'][name]['timeout'] == 86400000
    upgraded['mcpServers'][name]['timeout'] = 90000000
    path.write_text(json.dumps(upgraded))
    install_profile(root, 'shop', 'qa', 'primary', 'claude-code', home)
    preserved = json.loads(path.read_text())
    assert preserved['mcpServers'][name]['timeout'] == 90000000
    assert preserved['unrelated'] == {'keep':True}


def test_the_card_and_every_setup_prompt_point_at_the_wait_tool(desktop):
    """A role chat that narrates empty waits grows its context until it stops."""
    from hoh_control_plane.local_agent import make_prompts

    client, root = desktop
    client.post(
        "/local/projects", json={"project_id": "shop", "name": "Shop"},
        headers=headers(client),
    )

    card = (
        Path(__file__).resolve().parents[1] / 'skills' / 'hoh-control-plane' / 'SKILL.md'
    ).read_text(encoding='utf-8')
    from hoh_control_plane.mcp.tools import common as mcp_server

    # The tool description is the one place the silent-wait rule is stated; the
    # card and every prompt point at it instead of restating it.
    source = Path(mcp_server.__file__).read_text(encoding='utf-8')
    assert "While waiting write nothing to the chat" in source
    assert "call this again at once" in source
    assert "as its tool" in card and "description says" in card

    for role, prompt in make_prompts(root, "shop", instance="codex-1").items():
        assert "as its tool" in prompt and "description says" in prompt, role
        assert "resume_wait_id" in prompt, role


def test_execution_prompts_make_the_chat_a_dispatcher(desktop):
    """Each ticket goes to a fresh worker, so no transcript reaches the next ticket."""
    from hoh_control_plane.local_agent import make_prompts

    client, root = desktop
    client.post(
        "/local/projects", json={"project_id": "shop", "name": "Shop"},
        headers=headers(client),
    )
    prompts = make_prompts(root, "shop", instance="codex-1")

    for role in ("developer", "qa", "integrator"):
        assert "worker_brief" in prompts[role], role
        assert "spawn_agent" in prompts[role] and "new session per ticket" in prompts[role]
    assert "worker_brief" not in prompts["planner"]


def test_setup_pins_a_model_where_the_client_has_a_setting(desktop, tmp_path):
    """Codex gets a profile model; Claude Code gets a worker subagent with one."""
    import tomllib
    from hoh_control_plane.local_agent import install_profile

    client, root = desktop
    client.post('/local/projects', json={'project_id': 'shop', 'name': 'Shop'}, headers=headers(client))

    codex_home = tmp_path / 'codex-home'
    codex = install_profile(root, 'shop', 'planner', 'primary', 'codex', codex_home, model='strong-model')
    config = tomllib.loads((codex_home / '.codex/config.toml').read_text())
    assert config['profiles'][codex['server_name']] == {'model': 'strong-model'}
    assert codex['codex_profile'] == codex['server_name']
    # An identical rerun is a no-op; a different model is refused, not overwritten.
    install_profile(root, 'shop', 'planner', 'primary', 'codex', codex_home, model='strong-model')
    with pytest.raises(ValueError, match='profile differs'):
        install_profile(root, 'shop', 'planner', 'primary', 'codex', codex_home, model='other-model')

    claude_home = tmp_path / 'claude-home'
    qa = install_profile(root, 'shop', 'qa', 'primary', 'claude-code', claude_home, model='checker-model')
    agent = claude_home / '.claude/agents' / f"{qa['server_name']}-worker.md"
    text = agent.read_text(encoding='utf-8')
    assert text.startswith('---\nname: ' + qa['claude_agent'] + '\n')
    assert '\nmodel: checker-model\n' in text
    assert qa['server_name'] in text and 'worker_brief' in text
    planner = install_profile(root, 'shop', 'planner', 'primary', 'claude-code', claude_home, model='strong-model')
    assert 'model_note' in planner and 'claude_agent' not in planner
    assert sorted(path.name for path in (claude_home / '.claude/agents').iterdir()) == [agent.name]

    with pytest.raises(ValueError, match='plain model identifier'):
        install_profile(root, 'shop', 'qa', 'primary', 'codex', tmp_path / 'x', model='bad model; rm')
    plain = install_profile(root, 'shop', 'developer', 'primary', 'codex', tmp_path / 'plain')
    assert 'model' not in plain
    assert 'profiles' not in tomllib.loads((tmp_path / 'plain/.codex/config.toml').read_text())
