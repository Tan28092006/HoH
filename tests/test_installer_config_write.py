"""P2: the kit installer writes the client config atomically, under a lock.

``~/.claude.json`` holds tokens. The installer must never leave a copy of it
beside the original, must never leave a half-written file, and must not drop
what the client itself saved between the installer's first read and its write.
Every test uses a temporary home; the real one is never touched.
"""
from __future__ import annotations

import importlib.util
import json
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest

from hoh_control_plane.agent_kit import build_kit

TOKEN = "sk-test-token-never-copied"


def _kit(tmp_path: Path):
    output = tmp_path / "kit"
    build_kit(output, "developer", "https://hoh.example.invalid/mcp")
    spec = importlib.util.spec_from_file_location("kit_installer_p2", output / "install.py")
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    home = tmp_path / "home"
    home.mkdir()
    config = home / ".claude.json"
    config.write_text(json.dumps({"oauthAccount": {"token": TOKEN},
                                  "mcpServers": {"existing": {"command": "keep-me"}}}),
                      encoding="utf-8")
    return installer, output, home, config


def _token_copies(home: Path, config: Path) -> list[Path]:
    return [path for path in home.iterdir()
            if path.is_file() and path != config and TOKEN.encode() in path.read_bytes()]


def test_install_leaves_no_token_bearing_copy_and_removes_old_backups(tmp_path):
    installer, kit, home, config = _kit(tmp_path)
    stale = home / ".claude.json.hoh-backup-abc123"
    stale.write_text(config.read_text(encoding="utf-8"), encoding="utf-8")

    installer.install(kit, home, "claude-code")

    parsed = json.loads(config.read_text(encoding="utf-8"))
    assert set(parsed["mcpServers"]) == {"existing", "hoh-developer"}
    assert parsed["oauthAccount"]["token"] == TOKEN
    assert not stale.exists()
    assert _token_copies(home, config) == []


def test_a_failed_replace_keeps_the_original_and_no_temporary_file(tmp_path, monkeypatch):
    installer, kit, home, config = _kit(tmp_path)
    original = config.read_bytes()

    def refuse(_source, _target):
        raise PermissionError("the client holds the file")

    monkeypatch.setattr(installer, "_REPLACE_ATTEMPTS", 2)
    monkeypatch.setattr(installer.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(installer.os, "replace", refuse)
    with pytest.raises(PermissionError):
        installer.install(kit, home, "claude-code")

    assert config.read_bytes() == original
    assert _token_copies(home, config) == []
    assert not list(home.glob(".claude.json.hoh-tmp-*"))


def test_a_transient_sharing_violation_is_retried(tmp_path, monkeypatch):
    installer, kit, home, config = _kit(tmp_path)
    real_replace = installer.os.replace
    refusals = []

    def busy_once(source, target):
        if not refusals:
            refusals.append(target)
            raise PermissionError("the client holds the file")
        real_replace(source, target)

    monkeypatch.setattr(installer.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(installer.os, "replace", busy_once)
    installer.install(kit, home, "claude-code")

    assert "hoh-developer" in json.loads(config.read_text(encoding="utf-8"))["mcpServers"]
    assert len(refusals) == 1


def test_what_the_client_saved_meanwhile_is_merged_not_overwritten(tmp_path, monkeypatch):
    installer, kit, home, config = _kit(tmp_path)
    real_lock = installer._config_lock

    @contextmanager
    def client_saves_first(path):
        # The client rewrites its own config after the installer's first read.
        saved = json.loads(path.read_text(encoding="utf-8"))
        saved["projects"] = {"D:/work": {"allowedTools": []}}
        path.write_text(json.dumps(saved), encoding="utf-8")
        with real_lock(path):
            yield

    monkeypatch.setattr(installer, "_config_lock", client_saves_first)
    installer.install(kit, home, "claude-code")

    parsed = json.loads(config.read_text(encoding="utf-8"))
    assert parsed["projects"] == {"D:/work": {"allowedTools": []}}
    assert set(parsed["mcpServers"]) == {"existing", "hoh-developer"}


def test_a_second_installer_waits_for_the_lock_then_gives_up(tmp_path, monkeypatch):
    installer, kit, home, config = _kit(tmp_path)
    original = config.read_bytes()
    monkeypatch.setattr(installer, "_LOCK_WAIT_SECONDS", 0.3)
    held, release = threading.Event(), threading.Event()

    def hold():
        with installer._config_lock(config):
            held.set()
            release.wait(10)

    holder = threading.Thread(target=hold)
    holder.start()
    try:
        assert held.wait(10)
        with pytest.raises(ValueError, match="Another HoH install"):
            installer.install(kit, home, "claude-code")
        assert config.read_bytes() == original
    finally:
        release.set()
        holder.join(10)

    # Once released, the next install proceeds.
    installer.install(kit, home, "claude-code")
    assert "hoh-developer" in json.loads(config.read_text(encoding="utf-8"))["mcpServers"]


def test_a_conflicting_config_refuses_before_any_write(tmp_path):
    installer, kit, home, config = _kit(tmp_path)
    config.write_text(json.dumps({"mcpServers": {"hoh-developer": {"command": "someone-else"}}}),
                      encoding="utf-8")
    original = config.read_bytes()

    with pytest.raises(ValueError, match="differs"):
        installer.install(kit, home, "claude-code")

    assert config.read_bytes() == original
    assert not (home / ".claude" / "skills").exists()
