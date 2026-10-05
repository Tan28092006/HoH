import importlib.util
import json
import tomllib
from pathlib import Path
from zipfile import ZipFile

import pytest

from hoh_control_plane.agent_kit import build_kit


@pytest.mark.parametrize("role", ["planner", "developer", "qa", "integrator"])
@pytest.mark.parametrize("client", ["codex", "claude-code"])
def test_portable_kit_installs_and_replays_without_touching_other_servers(tmp_path, role, client):
    output = tmp_path / "kit"
    archive = build_kit(output, role, "https://hoh.example.invalid/mcp")
    with ZipFile(archive) as bundle:
        assert "skills/hoh-control-plane/references/session-start.md" in bundle.namelist()
    home = tmp_path / "home"
    home.mkdir()
    if client == "codex":
        config = home / ".codex/config.toml"
        config.parent.mkdir()
        config.write_text('[mcp_servers.existing]\ncommand="keep-me"\n', encoding="utf-8")
    else:
        config = home / ".claude.json"
        config.write_text('{"mcpServers":{"existing":{"command":"keep-me"}}}', encoding="utf-8")
    spec = importlib.util.spec_from_file_location("kit_installer", output / "install.py")
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    installer.install(output, home, client)
    first = config.read_bytes()
    installer.install(output, home, client)
    assert config.read_bytes() == first
    assert b"keep-me" in first
    parsed = tomllib.loads(first.decode()) if client == "codex" else json.loads(first)
    servers = parsed.get("mcp_servers", parsed.get("mcpServers"))
    assert set(servers) == {"existing", f"hoh-{role}"}
    skill = home / (".codex" if client == "codex" else ".claude") / "skills" / f"hoh-{role}"
    assert (skill / "SKILL.md").is_file()


@pytest.mark.parametrize("url", ["http://remote/mcp", "https://user:secret@host/mcp", "https://host/mcp?token=secret"])
def test_kit_refuses_credential_bearing_or_insecure_urls(tmp_path, url):
    with pytest.raises(ValueError):
        build_kit(tmp_path / "kit", "developer", url)
    assert not (tmp_path / "kit").exists()


def test_installer_rejects_tampered_asset_before_mutating_home(tmp_path):
    output, home = tmp_path / "kit", tmp_path / "home"
    build_kit(output, "qa", "http://127.0.0.1:8931/mcp")
    (output / "skills/hoh-qa/SKILL.md").write_text("tampered")
    spec = importlib.util.spec_from_file_location("kit_installer", output / "install.py")
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    with pytest.raises(ValueError, match="checksum"):
        installer.install(output, home, "codex")
    assert not home.exists()


def test_start_here_tells_an_execution_session_to_dispatch(tmp_path):
    build_kit(tmp_path / "kit", "qa", "http://127.0.0.1:8931/mcp")
    start = (tmp_path / "kit" / "START_HERE.md").read_text(encoding="utf-8")

    assert "worker_brief" in start
    assert "new session per ticket" in start
