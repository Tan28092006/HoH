"""Wave 8A, R8: a candidate does not choose its own provenance oracle.

The packages a run must import from the frozen candidate are frozen in the
Planner-owned candidate test profile (the project's list, or what the base
commit declares). A run in which no test passed is not a passing run.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from hoh_control_plane.domain import ValidationError
from hoh_control_plane.qa_runner import (
    INFRASTRUCTURE_REASONS,
    ProjectTestRunner,
    provenance_modules_from,
)

_PYTEST = [sys.executable, "-I", "-m", "pytest", "-q", "-p", "no:cacheprovider"]


@pytest.fixture
def _ample_memory(monkeypatch):
    """The admission floor reads this host's free memory; these tests are not about it."""
    from hoh_control_plane import qa_runner

    monkeypatch.setattr(qa_runner, "resource_snapshot", lambda: {
        "available_physical_bytes": 64 * 1024**3, "available_commit_bytes": 64 * 1024**3})


def _candidate(tmp_path: Path, *, declared: list[str], pythonpath: str, test: str) -> Path:
    """A tree whose own pyproject declares ``declared`` as its provenance list."""
    root = tmp_path / "candidate"
    for package in ("sample", "trivial"):
        (root / "src" / package).mkdir(parents=True)
        (root / "src" / package / "__init__.py").write_text("value = 7\n")
    (root / "pyproject.toml").write_text(
        "[tool.hoh.qa]\nprovenance_modules = " + json.dumps(declared) + "\n"
        "[tool.pytest.ini_options]\npythonpath = " + json.dumps([pythonpath, "src"]) + "\n")
    (root / "test_sample.py").write_text(
        "import pytest, sample, trivial\ndef test_value():\n    " + test + "\n")
    return root


def test_a_candidate_that_narrows_its_own_list_cannot_hide_a_shadowed_package(tmp_path, _ample_memory):
    # Another tree on the candidate's pythonpath shadows `sample`.
    other = tmp_path / "editable-main"
    (other / "sample").mkdir(parents=True)
    (other / "sample/__init__.py").write_text("value = 7\n")
    root = _candidate(tmp_path, declared=["trivial"], pythonpath=str(other),
                      test="assert sample.value == 7")
    runner = ProjectTestRunner(tmp_path / "evidence")

    # The hole: trusting the candidate's own declaration, the run passes.
    trusted = runner.execute(_PYTEST, str(root), 60)
    assert trusted.termination_reason == "passed", trusted.stderr
    assert trusted.environment_manifest["provenance_modules_source"] == "candidate_tree"

    # With the frozen list, the shadowed package is caught.
    frozen = runner.execute(_PYTEST, str(root), 60, provenance_modules=["sample", "trivial"])
    assert frozen.artifact_mismatch is True
    assert frozen.termination_reason == "artifact_mismatch"
    assert frozen.environment_manifest["provenance_modules"] == ["sample", "trivial"]
    assert frozen.environment_manifest["provenance_modules_source"] == "frozen_profile"
    assert frozen.environment_manifest["candidate_declared_modules"] == ["trivial"]


def test_a_run_where_every_test_was_skipped_is_not_a_pass(tmp_path, _ample_memory):
    root = _candidate(tmp_path, declared=["sample"], pythonpath="src",
                      test="pytest.skip('skipped by the candidate')")
    execution = ProjectTestRunner(tmp_path / "evidence").execute(
        _PYTEST, str(root), 60, provenance_modules=["sample"])
    assert execution.exit_code == 0, execution.stderr
    assert execution.termination_reason == "no_tests_passed"
    # The candidate answers for it: not an infrastructure retry.
    assert "no_tests_passed" not in INFRASTRUCTURE_REASONS
    assert execution.environment_manifest["classification"] == "candidate_failure"
    # One real test passing is enough.
    (root / "test_sample.py").write_text("import sample\ndef test_value():\n    assert sample.value == 7\n")
    passing = ProjectTestRunner(tmp_path / "evidence").execute(
        _PYTEST, str(root), 60, provenance_modules=["sample"])
    assert passing.termination_reason == "passed", passing.stderr


def test_the_declared_list_prefers_explicit_config_then_wheel_then_src():
    explicit = '[tool.hoh.qa]\nprovenance_modules = ["a.b"]\n[tool.hatch.build.targets.wheel]\npackages = ["src/c"]\n'
    assert provenance_modules_from(explicit, ["d"]) == ["a.b"]
    assert provenance_modules_from('[tool.hatch.build.targets.wheel]\npackages = ["src/c"]\n', ["d"]) == ["c"]
    assert provenance_modules_from(None, ["e", "d", "d"]) == ["d", "e"]
    assert provenance_modules_from(None, []) == []
    for bad in ('[tool.hoh.qa]\nprovenance_modules = ["os; rm"]\n', "not = [toml"):
        with pytest.raises(ValidationError):
            provenance_modules_from(bad, [])


# --- the service freezes the list --------------------------------------------

def _git(path: Path, *arguments: str) -> str:
    return subprocess.run(["git", "-C", str(path), *arguments], check=True,
                          capture_output=True, text=True).stdout.strip()


def _repository_declaring(tmp_path: Path, modules: list[str]) -> Path:
    repository = tmp_path / "checkout-repository"
    (repository / "src" / "checkout").mkdir(parents=True)
    _git(repository, "init", "--initial-branch=main")
    _git(repository, "config", "user.email", "hoh-tests@example.invalid")
    _git(repository, "config", "user.name", "HoH Tests")
    (repository / "src/checkout/__init__.py").write_text("")
    (repository / "pyproject.toml").write_text(
        "[tool.hoh.qa]\nprovenance_modules = " + json.dumps(modules) + "\n")
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "Initial checkout")
    return repository


def _submit(service, actors, seeded, repository: Path, **configure):
    service.configure_repository(actors["planner"], "project-1", str(repository))
    service.configure_test_command(actors["planner"], "project-1",
                                   [sys.executable, "-c", "print('passed')"], 120, **configure)
    ticket = seeded["ticket"]
    development = service.claim_ticket(actors["developer"], ticket["ticket_id"], ticket["version"], 900)
    prepared = service.prepare_worktree(actors["developer"], ticket["ticket_id"],
                                        development["lease"]["lease_id"], development["ticket"]["version"])
    worktree = Path(prepared["worktree"]["worktree_path"])
    (worktree / "src/checkout/retry.txt").write_text("safe retry\n")
    _git(worktree, "add", "src/checkout/retry.txt")
    _git(worktree, "commit", "-m", "Implement safe retry")
    candidate = service.submit_candidate(
        actors["developer"], ticket["ticket_id"], development["lease"]["lease_id"],
        development["ticket"]["version"], _git(worktree, "rev-parse", "HEAD"),
        prepared["worktree"]["base_commit"], prepared["worktree"]["branch_ref"], ["Ready."],
        context_snapshot_id=service.get_ticket_context(
            actors["developer"], ticket["ticket_id"])["context_snapshot_id"])
    return candidate["candidate"]["candidate_id"]


def _frozen(service, candidate_id):
    with service.store.connection() as connection:
        row = connection.execute(
            "SELECT provenance_modules FROM candidate_test_profiles WHERE candidate_id = ?",
            (candidate_id,)).fetchone()
    return None if row["provenance_modules"] is None else json.loads(row["provenance_modules"])


def test_a_candidate_freezes_what_its_base_commit_declares(service, actors, seeded, tmp_path, monkeypatch):
    repository = _repository_declaring(tmp_path, ["checkout"])
    candidate_id = _submit(service, actors, seeded, repository)
    assert _frozen(service, candidate_id) == ["checkout"]
    assert service.get_test_profile(actors["planner"], "project-1")["provenance_modules"] is None

    seen = []
    monkeypatch.setattr(service.project_tests, "execute",
                        lambda *a, **k: seen.append(k) or "ran")
    assert service._execute_frozen_profile(["x"], "path", 5, candidate_id) == "ran"
    assert seen == [{"provenance_modules": ["checkout"]}]


def test_the_planner_list_overrides_the_base_declaration(service, actors, seeded, tmp_path):
    repository = _repository_declaring(tmp_path, ["checkout"])
    candidate_id = _submit(service, actors, seeded, repository,
                           provenance_modules=["checkout", "checkout.retry"])
    assert _frozen(service, candidate_id) == ["checkout", "checkout.retry"]
    assert service.get_test_profile(actors["planner"], "project-1")["provenance_modules"] == [
        "checkout", "checkout.retry"]
    with pytest.raises(ValidationError):
        service.configure_test_command(actors["planner"], "project-1", [sys.executable, "-V"], 60,
                                       provenance_modules=["not a module"])


def test_declared_modules_come_from_git_objects_not_a_checkout(service, tmp_path):
    repository = _repository_declaring(tmp_path, ["checkout"])
    base = _git(repository, "rev-parse", "HEAD")
    # The working tree now says something else; the commit is what counts.
    (repository / "pyproject.toml").write_text('[tool.hoh.qa]\nprovenance_modules = ["trivial"]\n')
    assert service.git_worktrees.declared_provenance_modules(
        repository_path=str(repository), commit=base) == ["checkout"]
    (repository / "pyproject.toml").unlink()
    _git(repository, "add", "-A")
    _git(repository, "commit", "-m", "Drop the pyproject")
    assert service.git_worktrees.declared_provenance_modules(
        repository_path=str(repository), commit="HEAD") == ["checkout"]  # from src/
