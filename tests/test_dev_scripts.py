"""The dev start scripts never open the real database or take the installed ports.

They used to point HOH_DATABASE_PATH at <repo>/data/control-plane.db, seed a
demo project and listen on 8765: the real database and the installed host's
port. Each refusal here happens before any Python process starts.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
POWERSHELL = shutil.which("powershell") or shutil.which("pwsh")

windows_only = pytest.mark.skipif(
    sys.platform != "win32" or POWERSHELL is None, reason="the dev scripts are Windows PowerShell"
)


def _run(script: str, *arguments: str, environment: dict[str, str] | None = None):
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("HOH_")}
    env.update(environment or {})
    return subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-File", str(SCRIPTS / script), *arguments],
        capture_output=True, text=True, timeout=120, env=env, cwd=ROOT,
    )


def _command(command: str, environment: dict[str, str] | None = None):
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("HOH_")}
    env.update(environment or {})
    return subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", command],
        capture_output=True, text=True, timeout=120, env=env, cwd=ROOT,
    )


@windows_only
@pytest.mark.parametrize("script", ["start-dashboard.ps1", "start-scheduler.ps1"])
@pytest.mark.parametrize("database", [
    r"D:\HoH\data\control-plane.db",
    r"D:\HOH\Data\control-plane.db",
    r"D:\HoH-service\data\control-plane.db",
])
def test_the_real_database_is_refused(script, database) -> None:
    result = _run(script, "-DatabasePath", database)

    assert result.returncode != 0
    assert "Refusing to start a dev instance" in result.stdout + result.stderr


@windows_only
def test_this_checkouts_data_directory_is_refused_through_the_environment() -> None:
    result = _run(
        "start-scheduler.ps1", "-Once",
        environment={"HOH_DEV_DATABASE_PATH": str(ROOT / "data" / "control-plane.db")},
    )

    assert result.returncode != 0
    assert "Refusing to start a dev instance" in result.stdout + result.stderr


@windows_only
@pytest.mark.parametrize("port", ["8765", "8931"])
def test_the_installed_hosts_ports_are_refused(port) -> None:
    result = _run("start-dashboard.ps1", "-Port", port)

    assert result.returncode != 0
    assert "belongs to the installed HoH host" in result.stdout + result.stderr


@windows_only
def test_a_dev_path_resolves_and_its_folder_is_created(tmp_path) -> None:
    target = tmp_path / "dev" / "control-plane.db"
    helper = SCRIPTS / "dev-environment.ps1"
    result = _command(
        f". '{helper}'; Resolve-HohDevDatabase -RepoRoot '{ROOT}'",
        environment={"HOH_DEV_DATABASE_PATH": str(target)},
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(target)
    assert target.parent.is_dir()
    assert not target.exists(), "resolving a path does not create a database"


def test_the_dev_defaults_are_not_the_installed_hosts() -> None:
    helper = (SCRIPTS / "dev-environment.ps1").read_text(encoding="utf-8")
    dashboard = (SCRIPTS / "start-dashboard.ps1").read_text(encoding="utf-8")

    assert '$HohDevDefaultDatabase = "D:\\HoH\\tmp\\dev\\control-plane.db"' in helper
    assert "[int]$Port = 8775" in dashboard
    for script in ("start-dashboard.ps1", "start-scheduler.ps1"):
        text = (SCRIPTS / script).read_text(encoding="utf-8")
        assert "Resolve-HohDevDatabase" in text, script
        assert "data\\control-plane.db" not in text, script
