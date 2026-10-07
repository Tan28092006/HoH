from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
import re
import tempfile
import tomllib

from hoh_control_plane.domain import ValidationError
from hoh_control_plane.managed_process import run_managed
from hoh_control_plane.resource_probe import resource_snapshot


_MAX_OUTPUT_CHARS = 200_000

# The frozen candidate always ships this project in a "src/" layout (see
# pyproject.toml's [tool.hatch.build.targets.wheel] packages setting), so the
# package importable from a worktree always lives at <worktree>/src/hoh_control_plane.
_CANDIDATE_PACKAGE = "hoh_control_plane"
_SOURCE_IDENTITY_PROBE = (
    f"import {_CANDIDATE_PACKAGE} as _m, sys; sys.stdout.write(_m.__file__)"
)
_PYTHON_INTERPRETER_NAMES = frozenset({"python", "python3", "python.exe", "pythonw.exe"})
_SOURCE_IDENTITY_PROBE_TIMEOUT_SECONDS = 30

# Wave 7A: a run that failed because of the host, not the candidate. These are
# recorded as infrastructure_failure, retried once with fewer xdist workers, and
# never count as a QA rejection.
INFRASTRUCTURE_REASONS = frozenset({
    "resource_unready", "resource_exhausted", "native_termination", "timeout", "cancelled",
    "capture_failure", "output_truncated", "launch_failure", "incomplete_evidence",
    "artifact_mismatch", "test_configuration_error", "pytest_interrupted",
    "pytest_internal_error", "pytest_usage_error", "foreign_import",
})
_RETRIED_REASONS = frozenset({
    "resource_exhausted", "native_termination", "pytest_interrupted",
    "pytest_internal_error", "pytest_usage_error", "foreign_import",
    "test_configuration_error",
})
# STATUS_COMMITMENT_LIMIT: Windows ran out of commit (pagefile) memory.
_OOM_EXIT_CODES = frozenset({0xC000012D, 0xC000012D - 2**32})
_MEMORY_MARKERS = ("MemoryError", "WinError 1455", "paging file is too small")
#: Rough peak commit per pytest-xdist worker, used to size "-n auto".
WORKER_MEMORY_BYTES = 600 * 1024**2


def xdist_workers(arguments: list[str]) -> str | None:
    """The requested xdist worker count ("auto" or a number), if any."""
    for index, argument in enumerate(arguments):
        if argument in ("-n", "--numprocesses") and index + 1 < len(arguments):
            return arguments[index + 1]
        if argument.startswith("--numprocesses="):
            return argument.split("=", 1)[1]
        if argument.startswith("-n") and len(argument) > 2 and not argument.startswith("--"):
            return argument[2:]
    return None


def with_xdist_workers(arguments: list[str], workers: int) -> list[str]:
    """The arguments with their xdist worker count replaced by ``workers``."""
    result: list[str] = []
    skip = False
    for index, argument in enumerate(arguments):
        if skip:
            skip = False
            continue
        if argument in ("-n", "--numprocesses") and index + 1 < len(arguments):
            result += [argument, str(workers)]
            skip = True
        elif argument.startswith("--numprocesses="):
            result.append(f"--numprocesses={workers}")
        elif argument.startswith("-n") and len(argument) > 2 and not argument.startswith("--"):
            result.append(f"-n{workers}")
        else:
            result.append(argument)
    return result


def memory_sized_workers(resources: dict, cpu_count: int | None = None) -> int:
    """Workers the host's free memory can hold: min(commit, physical) / 600 MB,
    capped at the CPU count, at least one."""
    cpus = cpu_count or os.cpu_count() or 1
    free = [
        resources[key] for key in ("available_commit_bytes", "available_physical_bytes")
        if isinstance(resources.get(key), int)
    ]
    if not free:
        return cpus
    return max(1, min(cpus, min(free) // WORKER_MEMORY_BYTES))


@dataclass(frozen=True, slots=True)
class TestExecution:
    command_digest: str
    exit_code: int | None
    timed_out: bool
    duration_ms: int
    stdout: str
    stderr: str
    # Populated whenever the configured command's interpreter could be identified as
    # Python: the hoh_control_plane.__file__ actually resolved in this execution's
    # environment, so QA evidence can show which source tree really ran.
    resolved_source: str | None = None
    # Digest of the facts that decide reproducibility: interpreter identity and
    # version, platform, the command, and the source tree that actually imported.
    # Two runs sharing this digest ran the same command in the same environment.
    environment_digest: str = ""

    # True only when that resolved source falls outside the frozen candidate worktree
    # (an ambient install, such as the shared venv's editable D:\HoH\src, won the
    # import instead). A mismatch is an infrastructure fault, not a candidate verdict:
    # the command below is never run, so exit_code/stdout/stderr carry no test result.
    artifact_mismatch: bool = False
    provenance_status: str = "unverified"
    termination_reason: str = "unclassified"
    environment_manifest: dict = field(default_factory=dict)
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    capture_errors: tuple[str, ...] = ()


_MODULE_NAME = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*")
_MAX_PROVENANCE_MODULES = 64


def validate_provenance_modules(modules) -> list[str]:
    if not isinstance(modules, list) or len(modules) > _MAX_PROVENANCE_MODULES or any(
        not isinstance(m, str) or not _MODULE_NAME.fullmatch(m) for m in modules
    ):
        raise ValidationError("Invalid frozen project provenance_modules configuration")
    return list(modules)


def provenance_modules_from(pyproject_text: str | None, src_packages: list[str]) -> list[str]:
    """The packages a tree declares for import attestation.

    [tool.hoh.qa].provenance_modules, then the wheel's packages, then the
    packages under src/. Applied to the candidate's base commit when the
    candidate is frozen, so the candidate's own edits cannot choose its oracle.
    """
    try:
        document = tomllib.loads(pyproject_text) if pyproject_text else {}
    except tomllib.TOMLDecodeError as error:
        raise ValidationError("pyproject.toml cannot be parsed for provenance_modules") from error
    tool = document.get("tool", {})
    modules = tool.get("hoh", {}).get("qa", {}).get("provenance_modules")
    if modules is None:
        packages = tool.get("hatch", {}).get("build", {}).get("targets", {}).get("wheel", {}).get("packages", [])
        modules = [Path(p).name for p in packages if isinstance(p, str)] if isinstance(packages, list) else packages
    if not modules:
        modules = sorted(set(src_packages))
    return validate_provenance_modules(modules)


class ProjectTestRunner:
    """Run only Planner-registered commands without a shell and retain bounded evidence."""

    def __init__(self, evidence_root: Path) -> None:
        self.evidence_root = evidence_root.resolve()

    def execute(
        self, command: list[str], worktree_path: str, timeout_seconds: int,
        provenance_modules: list[str] | None = None,
    ) -> TestExecution:
        """Run the registered command against the frozen worktree.

        ``provenance_modules`` is the list frozen in the candidate's test
        profile (wave 8A, R8). When given, the candidate's own pyproject.toml
        cannot change which packages the run must attest.
        """
        command_digest = hashlib.sha256("\0".join(command).encode()).hexdigest()
        started = time.perf_counter()
        environment = self._build_environment(worktree_path)
        if self._pytest_arguments(command) is not None:
            return self._execute_pytest(command, worktree_path, timeout_seconds, environment,
                                        frozen_modules=provenance_modules)
        resolved_source = self._resolve_candidate_source(command, worktree_path, environment)
        if resolved_source is not None and not self._within_worktree(
            resolved_source, worktree_path
        ):
            duration_ms = round((time.perf_counter() - started) * 1000)
            mismatch_detail = (
                f"candidate_artifact_mismatch: expected {_CANDIDATE_PACKAGE} to resolve "
                f"under {Path(worktree_path).resolve()}, observed {resolved_source}"
            )
            return TestExecution(
                command_digest=command_digest,
                exit_code=None,
                timed_out=False,
                duration_ms=duration_ms,
                stdout="",
                stderr=mismatch_detail,
                resolved_source=resolved_source,
                artifact_mismatch=True,
                environment_digest=self.environment_digest(command, resolved_source),
            )
        completed = None
        try:
            completed = run_managed(
                command,
                cwd=worktree_path,
                env=environment,
                timeout=timeout_seconds,
            )
            if completed.returncode == 127 and '[hoh:exec-error]' in completed.stderr:
                raise ValidationError('Registered project test command cannot be executed')
            exit_code = completed.returncode
            timed_out = completed.timed_out
            if timed_out:
                exit_code = None
            stdout = completed.stdout
            stderr = completed.stderr
        except subprocess.TimeoutExpired as error:
            exit_code = None
            timed_out = True
            stdout = self._as_text(error.stdout)
            stderr = self._as_text(error.stderr)
        except OSError as error:
            raise ValidationError("Registered project test command cannot be executed") from error
        duration_ms = round((time.perf_counter() - started) * 1000)
        capture_errors = getattr(completed, "capture_errors", ())
        stdout_truncated = getattr(completed, "stdout_truncated", False) or len(stdout) > _MAX_OUTPUT_CHARS
        stderr_truncated = getattr(completed, "stderr_truncated", False) or len(stderr) > _MAX_OUTPUT_CHARS
        reason = "passed" if exit_code == 0 else "test_failure"
        if getattr(completed, "cancelled", False):
            reason = "cancelled"
        elif timed_out:
            reason = "timeout"
        elif capture_errors:
            reason = "capture_failure"
        elif stdout_truncated or stderr_truncated:
            reason = "output_truncated"
        elif exit_code is not None and (exit_code < 0 or exit_code > 255):
            reason = "native_termination"
        return TestExecution(
            command_digest=command_digest,
            exit_code=exit_code,
            timed_out=timed_out,
            duration_ms=duration_ms,
            stdout=self._truncate(stdout),
            stderr=self._truncate(stderr),
            resolved_source=resolved_source,
            environment_digest=self.environment_digest(command, resolved_source),
            termination_reason=reason, capture_errors=capture_errors,
            stdout_truncated=stdout_truncated, stderr_truncated=stderr_truncated,
        )

    @staticmethod
    def environment_digest(command: list[str], resolved_source: str | None) -> str:
        """Identify the environment a run happened in, not the machine it ran on."""
        material = json.dumps(
            {
                "command": command,
                "python": sys.version.split()[0],
                "implementation": platform.python_implementation(),
                "platform": platform.system(),
                "resolved_source": resolved_source,
            },
            sort_keys=True,
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def write_report(
        self,
        *,
        candidate_id: str,
        test_run_id: str,
        execution: TestExecution,
        candidate_commit: str | None = None,
    ) -> tuple[str, str, str]:
        if not candidate_id.startswith("cand_") or not test_run_id.startswith("testrun_"):
            raise ValidationError("Test evidence identifier is invalid")
        report_path = (self.evidence_root / candidate_id / f"{test_run_id}.json").resolve()
        if not report_path.is_relative_to(self.evidence_root):
            raise ValidationError("Test evidence path is invalid")
        report_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "candidate_id": candidate_id,
            "test_run_id": test_run_id,
            "candidate_commit": candidate_commit,
            "recorded_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            **asdict(execution),
        }
        # Write the bytes rather than the text: write_text would translate newlines
        # on Windows, so the file on disk would no longer match the digest below
        # and every later verification of this artifact would fail as a mismatch.
        body = json.dumps(payload, indent=2).encode("utf-8")
        report_path.write_bytes(body)
        # Checksum the bytes actually retained, so a later reader can prove the
        # artifact behind a storage reference has not changed.
        checksum = hashlib.sha256(body).hexdigest()
        storage_ref = f"evidence://local/{candidate_id}/{test_run_id}.json"
        return storage_ref, str(report_path), checksum

    def _build_environment(self, worktree_path: str) -> dict[str, str]:
        environment = os.environ.copy()
        # A candidate test must not inherit the control-plane's bearer token map,
        # database DSN or agent identity. OS-level sandboxing is still required
        # for hostile code: removing env vars is not a filesystem boundary.
        for key in list(environment):
            if (key.upper().startswith(("HOH_", "COV_CORE", "COVERAGE"))
                    or key.upper() in {'PYTHONPATH', 'PYTHONHOME', 'VIRTUAL_ENV',
                                       'PYTEST_ADDOPTS', 'PYTEST_PLUGINS', 'PYTHONSTARTUP'}):
                environment.pop(key)
        for key in (
            "COVERAGE_PROCESS_START",
            "COV_CORE_SOURCE",
            "COV_CORE_CONFIG",
            "COV_CORE_DATAFILE",
            "COV_CORE_BRANCH",
            "COV_CORE_CONTEXT",
        ):
            environment.pop(key, None)
        # A shared virtualenv's editable install resolves this project's package from
        # wherever it was originally installed from, not from whichever worktree is on
        # disk. Put this worktree's own src/ ahead of that on PYTHONPATH so the command
        # imports the exact candidate being tested rather than an unrelated checkout.
        source_root = Path(worktree_path) / "src"
        if source_root.is_dir():
            environment['PYTHONPATH'] = str(source_root.resolve())
        environment['PYTHONNOUSERSITE'] = '1'
        return environment

    @classmethod
    def _pytest_arguments(cls, command):
        if not command or not cls._looks_like_python_interpreter(command[0]):
            return None
        try:
            index = command.index("-m")
        except ValueError:
            return None
        return (command[1:index], command[index + 2:]) if command[index + 1:index + 2] == ["pytest"] else None

    @staticmethod
    def _provenance_modules(worktree):
        """Explicit project config, then wheel packages, then src packages,
        as the given tree declares them."""
        root = Path(worktree)
        path = root / "pyproject.toml"
        source = root / "src"
        return provenance_modules_from(
            path.read_text(encoding="utf-8") if path.is_file() else None,
            [p.parent.name for p in source.glob("*/__init__.py")] if source.is_dir() else [])

    def _execute_pytest(self, command, worktree, timeout, environment, *, retry_of=None,
                        frozen_modules=None):
        flags, arguments = self._pytest_arguments(command)
        requested_workers = xdist_workers(arguments)
        started = time.perf_counter()
        digest = hashlib.sha256("\0".join(command).encode()).hexdigest()
        if frozen_modules is not None:
            # The Planner-owned profile decides what is attested; the candidate's
            # own declaration is retained only as a fact (wave 8A, R8).
            modules = validate_provenance_modules(frozen_modules)
            modules_source = "frozen_profile"
        else:
            modules = self._provenance_modules(worktree)
            modules_source = "candidate_tree"
        resources = resource_snapshot()
        manifest = {"schema_version": 1, "command": command, "command_digest": digest,
                    "provenance_modules": modules, "provenance_modules_source": modules_source,
                    "host_resources": resources,
                    "environment_inputs": {k: environment[k] for k in
                        ("PYTHONPATH", "PYTHONNOUSERSITE", "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS") if k in environment}}
        if frozen_modules is not None:
            try:
                declared = self._provenance_modules(worktree)
            except (ValidationError, ValueError, OSError):
                declared = None
            if declared != modules:
                manifest["candidate_declared_modules"] = declared
        if retry_of is not None:
            manifest["retry_of"] = retry_of
            arguments = retry_of["arguments"]
        elif requested_workers == "auto":
            # The frozen command stays as recorded; the run uses what memory holds.
            arguments = with_xdist_workers(arguments, memory_sized_workers(resources))
        effective_workers = xdist_workers(arguments)
        manifest["xdist_workers"] = {"requested": requested_workers, "effective": effective_workers}
        def result(reason, *, child=None, completed=None):
            child = child or {}
            manifest["child"] = child
            paths = [v["path"] for v in child.get("source_modules", {}).values()]
            provenance = child.get("provenance_status", "unverified")
            material = json.dumps(manifest, sort_keys=True).encode()
            return TestExecution(command_digest=digest,
                exit_code=(None if completed is None or completed.timed_out else completed.returncode),
                timed_out=bool(completed and completed.timed_out),
                duration_ms=round((time.perf_counter() - started) * 1000),
                stdout=completed.stdout if completed else "", stderr=completed.stderr if completed else reason,
                resolved_source=paths[0] if paths else None, environment_digest=hashlib.sha256(material).hexdigest(),
                artifact_mismatch=provenance in {"mismatch", "unresolved"}, provenance_status=provenance,
                termination_reason=reason, environment_manifest={
                    **manifest, "classification": (
                        "passed" if reason == "passed" else
                        "infrastructure_failure" if reason in INFRASTRUCTURE_REASONS else
                        "candidate_failure")},
                stdout_truncated=bool(completed and completed.stdout_truncated),
                stderr_truncated=bool(completed and completed.stderr_truncated),
                capture_errors=completed.capture_errors if completed else ())
        # This is an admission floor, not a promise that every test fits. Retain
        # the measured facts so a later allocation failure is diagnosable.
        if resources.get("available_physical_bytes", 2**63) < 256 * 1024**2 or resources.get(
            "available_commit_bytes", 2**63) < 512 * 1024**2:
            return result("resource_unready")
        probe = Path(__file__).with_name("pytest_probe.py").resolve()
        helper = Path(__file__).with_name("resource_probe.py").resolve()
        manifest["instrumentation"] = {"protocol": "pytest-hooks-v1",
            "probe_sha256": hashlib.sha256(probe.read_bytes()).hexdigest(),
            "resource_probe_sha256": hashlib.sha256(helper.read_bytes()).hexdigest(),
            "python_flags": flags, "pytest_arguments": arguments}
        # Temporary evidence is outside the candidate. The wrapper invokes the
        # original pytest args with an observer plugin and preserves Python flags.
        with tempfile.TemporaryDirectory(prefix="hoh-pytest-") as temporary:
            report = Path(temporary) / "child.json"
            config = Path(temporary) / "config.json"
            config.write_text(json.dumps({"modules": modules}), encoding="utf-8")
            invocation = [command[0], *flags, str(probe), str(report), str(config), *arguments]
            manifest["instrumentation"]["entrypoint"] = str(probe)
            try:
                completed = run_managed(invocation, cwd=worktree, env=environment, timeout=timeout)
            except (OSError, subprocess.TimeoutExpired):
                return result("launch_failure")
            child = {}
            if report.is_file() and report.stat().st_size <= 2_000_000:
                try:
                    child = json.loads(report.read_text(encoding="utf-8"))
                except (ValueError, OSError):
                    pass
            reason = "passed"
            if completed.cancelled:
                reason = "cancelled"
            elif completed.timed_out:
                reason = "timeout"
            elif completed.capture_errors:
                reason = "capture_failure"
            elif completed.stdout_truncated or completed.stderr_truncated:
                reason = "output_truncated"
            elif completed.returncode == 127 and "[hoh:exec-error]" in completed.stderr:
                reason = "launch_failure"
            elif completed.returncode in _OOM_EXIT_CODES or child.get("resource_failures") or any(
                marker in (completed.stderr or "")[-20_000:] or marker in (completed.stdout or "")[-20_000:]
                for marker in _MEMORY_MARKERS
            ) and completed.returncode != 0:
                reason = "resource_exhausted"
            elif completed.returncode < 0 or completed.returncode > 255:
                reason = "native_termination"
            elif child.get("provenance_status") in {"mismatch", "unresolved"}:
                reason = "artifact_mismatch"
            elif child.get("foreign_import_errors"):
                reason = "foreign_import"
            elif completed.returncode == 2 and not (child.get("test_counts") or {}).get("collection_errors"):
                reason = "pytest_interrupted"
            elif completed.returncode == 3:
                reason = "pytest_internal_error"
            elif completed.returncode == 4:
                reason = "pytest_usage_error"
            elif child.get("phase") != "session_finished":
                reason = "incomplete_evidence"
            elif completed.returncode != 0:
                # 1: tests failed; 2 here: the candidate's own collection errors.
                reason = "test_failure" if completed.returncode in (1, 2) else "test_configuration_error"
            elif child.get("provenance_status") not in {"verified", "not_applicable"}:
                reason = "incomplete_evidence"
            elif not (child.get("test_counts") or {}).get("passed"):
                # Exit 0 with nothing passed (every test skipped or deselected)
                # proves nothing; the candidate answers for it (wave 8A, R8).
                reason = "no_tests_passed"
        if retry_of is None and reason in _RETRIED_REASONS:
            # Once, with half the workers: an environment failure is retried by
            # the runner, never sent back to the Developer.
            current = effective_workers
            retry_arguments = arguments
            if current is not None and current.isdigit():
                retry_arguments = with_xdist_workers(arguments, max(1, int(current) // 2))
            return self._execute_pytest(command, worktree, timeout, environment,
                                        frozen_modules=frozen_modules, retry_of={
                "reason": reason, "exit_code": completed.returncode,
                "xdist_workers": current, "arguments": retry_arguments,
            })
        return result(reason, child=child, completed=completed)

    def _resolve_candidate_source(
        self, command: list[str], worktree_path: str, environment: dict[str, str]
    ) -> str | None:
        """Best-effort probe of what hoh_control_plane the real command would import.

        Returns None when the configured command's interpreter cannot be identified as
        Python, when the frozen worktree does not even ship this package under a src/
        layout, or when the probe itself cannot be run or times out -- in every such
        case the caller proceeds exactly as before this check existed, so a command for
        a differently-shaped project is never blocked by a check that does not apply.
        """
        if not command or not self._looks_like_python_interpreter(command[0]):
            return None
        package_root = Path(worktree_path) / "src" / _CANDIDATE_PACKAGE / "__init__.py"
        if not package_root.is_file():
            return None
        try:
            probe = run_managed(
                [command[0], "-c", _SOURCE_IDENTITY_PROBE],
                cwd=worktree_path,
                env=environment,
                timeout=_SOURCE_IDENTITY_PROBE_TIMEOUT_SECONDS,
            )
        except (subprocess.TimeoutExpired, OSError):
            return None
        if probe.returncode != 0 or not probe.stdout.strip():
            return None
        return probe.stdout.strip()

    @staticmethod
    def _looks_like_python_interpreter(executable: str) -> bool:
        name = Path(executable).name.lower()
        return name in _PYTHON_INTERPRETER_NAMES or bool(
            name.startswith("python3.") or name.startswith("python2.")
        )

    @staticmethod
    def _within_worktree(resolved_source: str, worktree_path: str) -> bool:
        try:
            return Path(resolved_source).resolve().is_relative_to(Path(worktree_path).resolve())
        except (OSError, ValueError):
            return False

    @staticmethod
    def _truncate(value: str) -> str:
        if len(value) <= _MAX_OUTPUT_CHARS:
            return value
        return f"{value[:_MAX_OUTPUT_CHARS]}\n[output truncated]"

    @staticmethod
    def _as_text(value: str | bytes | None) -> str:
        if value is None:
            return ""
        return value.decode(errors="replace") if isinstance(value, bytes) else value
