import json
import os
from pathlib import Path
import sys

import pytest

from hoh_control_plane.managed_process import run_managed
from hoh_control_plane.qa_runner import ProjectTestRunner


def test_candidate_environment_discards_host_python_and_keeps_worktree(tmp_path, monkeypatch):
    source = tmp_path / 'candidate/src'
    source.mkdir(parents=True)
    (source / 'candidate_identity.py').write_text('VALUE = 42')
    monkeypatch.setenv('PYTHONPATH', str(tmp_path / 'host-site-packages'))
    monkeypatch.setenv('PYTHONHOME', str(tmp_path / 'wrong-python'))
    monkeypatch.setenv('HOH_MCP_TOKENS_JSON', 'do-not-inherit')
    runner = ProjectTestRunner(tmp_path / 'evidence')
    result = runner.execute([sys.executable, '-c',
        'import candidate_identity as c, os; print(c.VALUE); '
        'assert not any(k in os.environ for k in ("PYTHONHOME", "HOH_MCP_TOKENS_JSON")); '
        'assert "host-site-packages" not in os.environ["PYTHONPATH"]'], str(source.parent), 20)
    assert result.exit_code == 0, result.stderr
    assert result.stdout.strip() == '42'


def test_actual_different_minor_candidate_interpreter_is_isolated(tmp_path, monkeypatch):
    interpreter = os.getenv('HOH_TEST_CANDIDATE_PYTHON')
    if not interpreter:
        pytest.skip('Set HOH_TEST_CANDIDATE_PYTHON for a cross-version installation check')
    import pydantic_core
    host_packages = str(Path(pydantic_core.__file__).parent.parent)
    monkeypatch.setenv('PYTHONPATH', host_packages)
    monkeypatch.setenv('PYTHONHOME', str(tmp_path / 'invalid-home'))
    result = ProjectTestRunner(tmp_path / 'evidence').execute([interpreter, '-c',
        'import sys, json, pydantic_core; print(json.dumps({"version":sys.version_info[:2], '
        '"source":pydantic_core.__file__}))'], str(tmp_path), 20)
    assert result.exit_code == 0, result.stderr
    actual = json.loads(result.stdout)
    assert actual['version'] != list(sys.version_info[:2])
    assert not Path(actual['source']).is_relative_to(host_packages)


@pytest.mark.skipif(os.name != 'nt', reason='Windows DWORD exit status')
@pytest.mark.parametrize('code', [1, 0xC0000005, 0xC0000409])
def test_windows_native_exit_status_survives_gate(tmp_path, code):
    result = run_managed([sys.executable, '-c',
        f'import ctypes; ctypes.windll.kernel32.ExitProcess({code})'],
        cwd=tmp_path, env=os.environ.copy(), timeout=15)
    assert result.returncode == code
