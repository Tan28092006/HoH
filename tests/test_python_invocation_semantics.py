"""Actual Python invocation, pytest evidence, and regression-consumer controls."""
from pathlib import Path
import subprocess
import sys

import pytest

from hoh_control_plane.qa_runner import ProjectTestRunner
from hoh_control_plane.service.integration import IntegrationMixin


@pytest.fixture
def invocation_tree(tmp_path, monkeypatch):
    monkeypatch.setenv('PYTEST_DISABLE_PLUGIN_AUTOLOAD', '1')
    root = tmp_path / 'candidate'
    (root / 'src' / 'sample').mkdir(parents=True)
    (root / 'src' / 'sample' / '__init__.py').write_text('value = 7\n', encoding='utf-8')
    (root / 'pytest.ini').write_text('[pytest]\npythonpath = src\n', encoding='utf-8')
    return root


def _passed(execution):
    return IntegrationMixin._run_passed({
        'exit_code': execution.exit_code, 'timed_out': execution.timed_out,
        'termination_reason': execution.termination_reason})


def _observed(execution, root, *, passed, skipped, failed=0, collection_errors=0):
    manifest = execution.environment_manifest
    assert manifest['provenance_modules_source'] == 'frozen_profile'
    assert manifest['provenance_modules'] == ['sample']
    child = manifest['child']
    assert child['phase'] == 'session_finished'
    assert child['provenance_status'] == 'verified'
    assert child['test_counts'] == {'passed': passed, 'skipped': skipped, 'failed': failed,
                                    'collection_errors': collection_errors}
    assert Path(child['source_modules']['sample']['path']).resolve() == (root / 'src/sample/__init__.py').resolve()


@pytest.mark.parametrize('flags', [['-B', '-m'], ['-Bm'], ['-Bmpytest']])
@pytest.mark.parametrize('skip', [True, False], ids=['all-skipped', 'real-pass'])
def test_grouped_and_separate_pytest_have_truthful_consumer_evidence(invocation_tree, tmp_path, flags, skip):
    root = invocation_tree
    (root / 'test_sample.py').write_text(
        'import sys, pytest, sample\ndef test_value():\n'
        '    assert sys.dont_write_bytecode and sample.value == 7\n'
        + ("    pytest.skip('private skip control')\n" if skip else '    assert 2 + 2 == 4\n'), encoding='utf-8')
    selector = flags if flags[-1].endswith('pytest') else [*flags, 'pytest']
    execution = ProjectTestRunner(tmp_path / 'reports').execute(
        [sys.executable, *selector, '-q', '-p', 'no:cacheprovider', 'test_sample.py'], str(root), 30,
        provenance_modules=['sample'])
    assert execution.exit_code == 0, execution.stderr
    assert _passed(execution) is (not skip), execution.termination_reason
    assert execution.termination_reason == ('no_tests_passed' if skip else 'passed')
    _observed(execution, root, passed=int(not skip), skipped=int(skip))
    assert execution.environment_manifest['instrumentation']['python_flags'] == ['-B']


@pytest.mark.parametrize('flags,safe,isolated', [
    (['-B'], False, False), (['-B', '-P'], True, False),
    (['-BP'], True, False), (['-BI'], True, True)])
def test_direct_and_observer_preserve_shadow_import_semantics(invocation_tree, tmp_path, flags, safe, isolated):
    root = invocation_tree
    marker = 'PRIVATE_ROOT_PYTEST_SHADOW'
    (root / 'pytest.py').write_text(f'print({marker!r})\nraise SystemExit(0)\n', encoding='utf-8')
    (root / 'test_sample.py').write_text(
        'import sys, sample\ndef test_value():\n'
        '    assert sample.value == 7 and sys.dont_write_bytecode\n'
        f'    assert bool(sys.flags.safe_path) is {safe!r}\n'
        f'    assert bool(sys.flags.isolated) is {isolated!r}\n', encoding='utf-8')
    runner = ProjectTestRunner(tmp_path / 'reports')
    command = [sys.executable, *flags, '-m', 'pytest', '-q', '-p', 'no:cacheprovider', 'test_sample.py']
    direct = subprocess.run(command, cwd=root, env=runner._build_environment(str(root)),
                            capture_output=True, text=True, timeout=30)
    execution = runner.execute(command, str(root), 30, provenance_modules=['sample'])
    assert direct.returncode == execution.exit_code == 0
    assert (marker in direct.stdout) is (not safe)
    assert (marker in execution.stdout) is (not safe)
    assert _passed(execution) is safe
    if safe:
        assert '1 passed' in direct.stdout and '1 passed' in execution.stdout
        assert execution.termination_reason == 'passed', execution.stderr
        _observed(execution, root, passed=1, skipped=0)
    else:
        assert execution.termination_reason == 'incomplete_evidence'
        assert execution.environment_manifest['child']['phase'] == 'starting'


@pytest.mark.parametrize('argv,expected', [
    (['-Bm', 'pytest', '-q'], (['-B'], ['-q'])),
    (['-BPmpytest', '-q'], (['-BP'], ['-q'])),
    (['-BW', 'ignore', '-m', 'pytest'], (['-BW', 'ignore'], [])),
    (['-BXdev', '-m', 'pytest'], (['-BXdev'], [])),
    (['-W', '-m', '-c', 'pass'], None),
    (['-X', '-m', '-c', 'pass'], None),
    (['-Bc', 'pass', '-m', 'pytest'], None),
    (['script.py', '-m', 'pytest'], None),
    (['--', '-m', 'pytest'], None),
])
def test_mode_stops_at_selector_operand_or_program_argv(argv, expected):
    assert ProjectTestRunner._pytest_arguments([sys.executable, *argv]) == expected


@pytest.mark.parametrize('selector', [['-B', '-m', 'pytest'], ['-Bm', 'pytest']])
@pytest.mark.parametrize('collection', [False, True], ids=['failed-test', 'collection-error'])
@pytest.mark.parametrize('override', [False, True], ids=['normal-exit', 'exit-zero-hook'])
def test_negative_observer_counts_cannot_pass(invocation_tree, tmp_path, selector, collection, override):
    root = invocation_tree
    code = 'import sample\ndef test_pass():\n    assert sample.value == 7\n'
    if collection:
        (root / 'test_bad.py').write_text("raise ValueError('private collection control')\n", encoding='utf-8')
    else:
        code += 'def test_fail():\n    assert sample.value == 8\n'
    (root / 'test_sample.py').write_text(code, encoding='utf-8')
    if override:
        (root / 'conftest.py').write_text(
            'import pytest\n@pytest.hookimpl(trylast=True)\n'
            'def pytest_sessionfinish(session, exitstatus):\n    session.exitstatus = 0\n', encoding='utf-8')
    execution = ProjectTestRunner(tmp_path / 'reports').execute(
        [sys.executable, *selector, '-q', '-p', 'no:cacheprovider', '--continue-on-collection-errors'],
        str(root), 30, provenance_modules=['sample'])
    assert execution.exit_code == (0 if override else 1), execution.stderr
    _observed(execution, root, passed=1, skipped=0, failed=int(not collection), collection_errors=int(collection))
    assert execution.environment_manifest['child']['pytest_exit_status'] == 1
    assert not _passed(execution), execution.termination_reason
    assert execution.termination_reason == 'test_failure'
    assert execution.environment_manifest['classification'] == 'candidate_failure'


@pytest.mark.parametrize('selector', [['-B', '-m', 'pytest'], ['-Bm', 'pytest']])
def test_recorded_interruption_cannot_pass_after_real_tests(invocation_tree, tmp_path, selector):
    root = invocation_tree
    (root / 'test_sample.py').write_text(
        'import sample\ndef test_pass():\n    assert sample.value == 7\n', encoding='utf-8')
    (root / 'conftest.py').write_text(
        'import pytest\n@pytest.hookimpl(hookwrapper=True)\n'
        'def pytest_runtestloop(session):\n    yield\n'
        "    pytest.exit('private project interruption after tests', returncode=2)\n"
        '@pytest.hookimpl(trylast=True)\n'
        'def pytest_sessionfinish(session, exitstatus):\n    session.exitstatus = 0\n', encoding='utf-8')
    execution = ProjectTestRunner(tmp_path / 'reports').execute(
        [sys.executable, *selector, '-q', '-p', 'no:cacheprovider'], str(root), 30,
        provenance_modules=['sample'])
    assert execution.exit_code == 0, execution.stderr
    _observed(execution, root, passed=1, skipped=0)
    assert execution.environment_manifest['child']['pytest_exit_status'] == 2
    assert not _passed(execution), execution.termination_reason
    assert execution.termination_reason == 'pytest_interrupted'
    assert execution.environment_manifest['classification'] == 'infrastructure_failure'


@pytest.mark.parametrize('selector,flags', [
    (['-B', '-x', '-m', 'pytest'], ['-B', '-x']),
    (['-Bxm', 'pytest'], ['-Bx']), (['-xBm', 'pytest'], ['-xB']),
    (['-Bxmpytest'], ['-Bx'])])
def test_direct_and_observer_preserve_interpreter_x(invocation_tree, tmp_path, selector, flags):
    root = invocation_tree
    (root / 'test_sample.py').write_text(
        'import sys, sample\ndef test_pass():\n'
        '    assert sys.dont_write_bytecode and sample.value == 7\n', encoding='utf-8')
    runner = ProjectTestRunner(tmp_path / 'reports')
    arguments = ['-q', '-p', 'no:cacheprovider', '-x', 'test_sample.py']
    command = [sys.executable, *selector, *arguments]
    direct = subprocess.run(command, cwd=root, env=runner._build_environment(str(root)),
                            capture_output=True, text=True, timeout=30)
    assert direct.returncode == 0 and '1 passed' in direct.stdout, direct.stderr
    execution = runner.execute(command, str(root), 30, provenance_modules=['sample'])
    assert execution.exit_code == 0, execution.stderr
    _observed(execution, root, passed=1, skipped=0)
    assert execution.environment_manifest['child']['pytest_exit_status'] == 0
    assert _passed(execution) and execution.termination_reason == 'passed'
    assert execution.environment_manifest['instrumentation']['python_flags'] == flags
    assert execution.environment_manifest['instrumentation']['pytest_arguments'] == arguments


@pytest.mark.parametrize('selector', [['-B', '-m', 'pytest'], ['-Bm', 'pytest']])
def test_pytest_trailing_x_still_stops_after_failure(invocation_tree, tmp_path, selector):
    root = invocation_tree
    (root / 'test_sample.py').write_text(
        'import sample\ndef test_0_pass():\n    assert sample.value == 7\n'
        'def test_1_fail():\n    assert sample.value == 8\n'
        'def test_2_not_reached():\n    assert sample.value == 7\n', encoding='utf-8')
    runner = ProjectTestRunner(tmp_path / 'reports')
    command = [sys.executable, *selector, '-q', '-p', 'no:cacheprovider', '-x', 'test_sample.py']
    direct = subprocess.run(command, cwd=root, env=runner._build_environment(str(root)),
                            capture_output=True, text=True, timeout=30)
    execution = runner.execute(command, str(root), 30, provenance_modules=['sample'])
    assert direct.returncode == execution.exit_code == 1
    assert '1 failed, 1 passed' in direct.stdout
    _observed(execution, root, passed=1, skipped=0, failed=1)
    assert execution.environment_manifest['child']['pytest_exit_status'] == 1
    assert not _passed(execution) and execution.termination_reason == 'test_failure'
    assert execution.environment_manifest['instrumentation']['pytest_arguments'][-2:] == ['-x', 'test_sample.py']
