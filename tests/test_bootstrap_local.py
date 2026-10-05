"""bootstrap-local.ps1 installs into the shared .venv-hoh only from D:/HoH on main.

Run from a ticket worktree, its `pip install -e` repointed the shared venv's
hoh-* console scripts at that worktree's code (the review found them running
0.3.11 while main was 0.3.33).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "bootstrap-local.ps1"
POWERSHELL = shutil.which("powershell") or shutil.which("pwsh")


def test_the_checkout_and_branch_are_checked_before_anything_is_installed() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    refusal = text.index("Refusing to bootstrap from $repoRoot.")
    branch = text.index("rev-parse --abbrev-ref HEAD")
    assert '$mainCheckout = "D:\\HoH"' in text
    assert refusal < branch < text.index('"venv"') < text.index('"install" "-e"')


@pytest.mark.skipif(sys.platform != "win32" or POWERSHELL is None, reason="Windows PowerShell script")
@pytest.mark.skipif(
    str(ROOT).rstrip("\\/").lower() == "d:\\hoh",
    reason="only a checkout other than D:/HoH can show the refusal without installing",
)
def test_a_worktree_is_refused_without_creating_a_venv() -> None:
    venv = ROOT / ".venv-hoh"
    existed = venv.exists()
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("HOH_")}

    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPT),
         "-PythonCommand", "definitely-not-a-python"],
        capture_output=True, text=True, timeout=120, env=env, cwd=ROOT,
    )

    assert result.returncode != 0
    assert "Refusing to bootstrap from" in result.stdout + result.stderr
    assert venv.exists() == existed, "no virtual environment was created or removed"
