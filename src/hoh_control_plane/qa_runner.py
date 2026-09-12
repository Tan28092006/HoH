from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from hoh_control_plane.domain import ValidationError


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


class ProjectTestRunner:
    """Run only Planner-registered commands without a shell and retain bounded evidence."""

    def __init__(self, evidence_root: Path) -> None:
        self.evidence_root = evidence_root.resolve()

    def execute(
        self, command: list[str], worktree_path: str, timeout_seconds: int
    ) -> TestExecution:
        command_digest = hashlib.sha256("\0".join(command).encode()).hexdigest()
        started = time.perf_counter()
        environment = self._build_environment(worktree_path)
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
        try:
            completed = subprocess.run(
                command,
                cwd=worktree_path,
                check=False,
                capture_output=True,
                text=True,
                errors="replace",
                env=environment,
                shell=False,
                timeout=timeout_seconds,
            )
            exit_code = completed.returncode
            timed_out = False
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
        return TestExecution(
            command_digest=command_digest,
            exit_code=exit_code,
            timed_out=timed_out,
            duration_ms=duration_ms,
            stdout=self._truncate(stdout),
            stderr=self._truncate(stderr),
            resolved_source=resolved_source,
            environment_digest=self.environment_digest(command, resolved_source),
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
            if key.upper().startswith("HOH_"):
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
            existing_path = environment.get("PYTHONPATH", "")
            environment["PYTHONPATH"] = (
                f"{source_root}{os.pathsep}{existing_path}" if existing_path else str(source_root)
            )
        return environment

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
            probe = subprocess.run(
                [command[0], "-c", _SOURCE_IDENTITY_PROBE],
                cwd=worktree_path,
                check=False,
                capture_output=True,
                text=True,
                errors="replace",
                env=environment,
                shell=False,
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
