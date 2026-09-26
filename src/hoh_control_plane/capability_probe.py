"""Host-run capability preflight: HoH observes the equipment, not the QA agent.

A capability approval must rest on evidence the QA agent did not author. The
evidence therefore comes from this module, run by the HoH host: each probe
either succeeds on this machine or it does not, and only the outcome, a reason
code and a digest of the output are kept. Raw output, environment values and
local paths never leave the probe.

A probe can only speak for the machine HoH is running on. A QA agent that
registered from another machine gets every capability reported unproven with a
reason saying so, which routes the gap to infrastructure rather than to a
Planner approval nobody can justify.
"""
from __future__ import annotations

import hashlib
import os
import platform
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROBE_TIMEOUT_SECONDS = 60

# Files whose presence at the repository root proves a dependency manifest.
_MANIFESTS = (
    "package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock",
    "pyproject.toml", "requirements.txt", "poetry.lock", "uv.lock", "Pipfile",
    "go.mod", "Cargo.toml", "pom.xml", "build.gradle", "build.gradle.kts",
)

# Playwright npm packages; any one of them installed in the repository lets a
# Node project drive a browser.
_NODE_PLAYWRIGHT = ("playwright", "playwright-core", "@playwright/test")


@dataclass(frozen=True)
class ProbeOutcome:
    proven: bool
    reason: str
    method: str
    duration_ms: int = 0
    output_sha256: str | None = None

    def as_record(self) -> dict[str, Any]:
        record: dict[str, Any] = {
            "proven": self.proven,
            "reason": self.reason,
            "method": self.method,
            "duration_ms": self.duration_ms,
        }
        if self.output_sha256:
            record["output_sha256"] = self.output_sha256
        return record


class HostCapabilityProber:
    """Run each capability's preflight on this host.

    Replaceable, like the Git seam, so tests can state what a machine has
    without depending on the machine the suite happens to run on.
    """

    def host_names(self) -> set[str]:
        names = {socket.gethostname(), platform.node(), os.environ.get("COMPUTERNAME", "")}
        return {name.strip().lower() for name in names if name and name.strip()}

    def probe(
        self,
        capability: str,
        *,
        repository_path: str | None,
        test_command: list[str] | None,
        configured_command: list[str] | None,
    ) -> ProbeOutcome:
        if configured_command:
            return self._run("configured_command", configured_command, repository_path)
        builtin = getattr(self, "_probe_" + capability, None)
        if builtin is None:
            return ProbeOutcome(False, "no_preflight_defined", "none")
        return builtin(repository_path=repository_path, test_command=test_command)

    # ------------------------------------------------------------ built-ins

    def _probe_source_inspection(self, *, repository_path, test_command) -> ProbeOutcome:
        if not repository_path:
            return ProbeOutcome(False, "repository_not_configured", "builtin:git")
        return self._run("builtin:git", ["git", "-C", repository_path, "rev-parse", "--verify", "HEAD"], None)

    def _probe_git_worktree(self, *, repository_path, test_command) -> ProbeOutcome:
        if not repository_path:
            return ProbeOutcome(False, "repository_not_configured", "builtin:git")
        return self._run("builtin:git", ["git", "-C", repository_path, "worktree", "list", "--porcelain"], None)

    def _probe_dependency_manifest(self, *, repository_path, test_command) -> ProbeOutcome:
        if not repository_path:
            return ProbeOutcome(False, "repository_not_configured", "builtin:manifest")
        root = Path(repository_path)
        present = [name for name in _MANIFESTS if (root / name).is_file()]
        if present:
            return ProbeOutcome(True, "manifest_present", "builtin:manifest")
        return ProbeOutcome(False, "manifest_missing", "builtin:manifest")

    def _probe_build_execution(self, *, repository_path, test_command) -> ProbeOutcome:
        if not test_command:
            return ProbeOutcome(False, "test_command_not_configured", "builtin:test_command")
        executable = self._resolve(test_command[0], repository_path)
        if executable is None:
            return ProbeOutcome(False, "test_executable_missing", "builtin:test_command")
        return self._run("builtin:test_command", [executable, "--version"], repository_path)

    def _probe_browser_interaction(self, *, repository_path, test_command) -> ProbeOutcome:
        if not self._browser_build_installed():
            return ProbeOutcome(False, "browser_runtime_missing", "builtin:browser")
        if repository_path and any(
            (Path(repository_path) / "node_modules" / package / "package.json").is_file()
            for package in _NODE_PLAYWRIGHT
        ):
            return ProbeOutcome(True, "browser_runtime_and_node_driver", "builtin:browser")
        if test_command:
            interpreter = self._resolve(test_command[0], repository_path)
            if interpreter and Path(interpreter).name.lower().startswith("python"):
                outcome = self._run(
                    "builtin:browser", [interpreter, "-c", "import playwright.sync_api"],
                    repository_path,
                )
                if outcome.proven:
                    return ProbeOutcome(True, "browser_runtime_and_python_driver",
                                        "builtin:browser", outcome.duration_ms)
        return ProbeOutcome(False, "browser_driver_missing", "builtin:browser")

    # A screenshot is taken by the same browser runtime.
    _probe_screenshot = _probe_browser_interaction

    # --------------------------------------------------------------- helpers

    @staticmethod
    def _browser_build_installed() -> bool:
        roots = []
        configured = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
        if configured and configured != "0":
            roots.append(Path(configured))
        local = os.environ.get("LOCALAPPDATA")
        if local:
            roots.append(Path(local) / "ms-playwright")
        roots.append(Path.home() / ".cache" / "ms-playwright")
        for root in roots:
            if root.is_dir() and any(
                entry.is_dir() and entry.name.split("-")[0] in {"chromium", "firefox", "webkit", "msedge", "chrome"}
                for entry in root.iterdir()
            ):
                return True
        return False

    @staticmethod
    def _resolve(executable: str, repository_path: str | None) -> str | None:
        candidate = Path(executable)
        if not candidate.is_absolute() and repository_path:
            relative = Path(repository_path) / candidate
            if relative.is_file():
                return str(relative)
        if candidate.is_absolute():
            return str(candidate) if candidate.is_file() else None
        return shutil.which(executable)

    @staticmethod
    def _run(method: str, command: list[str], cwd: str | None) -> ProbeOutcome:
        started = time.monotonic()
        try:
            completed = subprocess.run(
                command, cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True,
                timeout=PROBE_TIMEOUT_SECONDS, check=False,
            )
        except FileNotFoundError:
            return ProbeOutcome(False, "executable_missing", method)
        except subprocess.TimeoutExpired:
            return ProbeOutcome(False, "timed_out", method, PROBE_TIMEOUT_SECONDS * 1000)
        except OSError:
            return ProbeOutcome(False, "could_not_start", method)
        duration = int((time.monotonic() - started) * 1000)
        # The digest identifies what was observed without keeping the output,
        # which may name local paths.
        digest = hashlib.sha256(completed.stdout + completed.stderr).hexdigest()
        if completed.returncode == 0:
            return ProbeOutcome(True, "exit_zero", method, duration, digest)
        return ProbeOutcome(False, f"exit_{completed.returncode}", method, duration, digest)


def machine_fingerprint(machine_id: str | None) -> str | None:
    """A stable, non-identifying token for comparing machines."""
    if not machine_id or not machine_id.strip():
        return None
    return hashlib.sha256(machine_id.strip().lower().encode("utf-8")).hexdigest()[:16]
