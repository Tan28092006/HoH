from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from hoh_control_plane.domain import ValidationError


_MAX_OUTPUT_CHARS = 200_000


@dataclass(frozen=True, slots=True)
class TestExecution:
    command_digest: str
    exit_code: int | None
    timed_out: bool
    duration_ms: int
    stdout: str
    stderr: str


class ProjectTestRunner:
    """Run only Planner-registered commands without a shell and retain bounded evidence."""

    def __init__(self, evidence_root: Path) -> None:
        self.evidence_root = evidence_root.resolve()

    def execute(
        self, command: list[str], worktree_path: str, timeout_seconds: int
    ) -> TestExecution:
        command_digest = hashlib.sha256("\0".join(command).encode()).hexdigest()
        started = time.perf_counter()
        environment = os.environ.copy()
        for key in (
            "COVERAGE_PROCESS_START",
            "COV_CORE_SOURCE",
            "COV_CORE_CONFIG",
            "COV_CORE_DATAFILE",
            "COV_CORE_BRANCH",
            "COV_CORE_CONTEXT",
        ):
            environment.pop(key, None)
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
        )

    def write_report(
        self,
        *,
        candidate_id: str,
        test_run_id: str,
        execution: TestExecution,
    ) -> tuple[str, str]:
        if not candidate_id.startswith("cand_") or not test_run_id.startswith("testrun_"):
            raise ValidationError("Test evidence identifier is invalid")
        report_path = (self.evidence_root / candidate_id / f"{test_run_id}.json").resolve()
        if not report_path.is_relative_to(self.evidence_root):
            raise ValidationError("Test evidence path is invalid")
        report_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "candidate_id": candidate_id,
            "test_run_id": test_run_id,
            "recorded_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            **asdict(execution),
        }
        report_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        storage_ref = f"evidence://local/{candidate_id}/{test_run_id}.json"
        return storage_ref, str(report_path)

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
