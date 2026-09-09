from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from hoh_control_plane.domain import ValidationError


_SAFE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


@dataclass(frozen=True, slots=True)
class PreparedWorktree:
    repository_path: str
    worktree_path: str
    branch_ref: str
    base_commit: str


class GitWorktreeManager:
    """Create and verify ticket-bound Git worktrees without invoking a shell."""

    def __init__(self, worktree_root: Path) -> None:
        self.worktree_root = worktree_root.resolve()

    def validate_repository(self, repository_path: str) -> str:
        path = self._existing_directory(repository_path, "repository_path")
        result = self._git(path, "rev-parse", "--show-toplevel", check=False)
        if result.returncode != 0 or not result.stdout.strip():
            raise ValidationError("repository_path must reference a Git repository")
        return str(Path(result.stdout.strip()).resolve())

    def prepare(
        self,
        *,
        repository_path: str,
        default_branch: str,
        project_id: str,
        ticket_id: str,
        agent_id: str,
    ) -> PreparedWorktree:
        repository = Path(self.validate_repository(repository_path))
        branch_ref = self._branch_ref(project_id, ticket_id, agent_id)
        base_commit = self._resolve_commit(repository, f"refs/heads/{default_branch}")
        worktree_path = self._worktree_path(project_id, ticket_id, agent_id)
        if worktree_path.exists():
            raise ValidationError("Assigned worktree path already exists")
        branch_exists = self._git(
            repository, "show-ref", "--verify", "--quiet", f"refs/heads/{branch_ref}", check=False
        )
        if branch_exists.returncode == 0:
            raise ValidationError("Assigned worktree branch already exists")
        if branch_exists.returncode not in {0, 1}:
            raise ValidationError("Unable to inspect the assigned worktree branch")

        worktree_path.parent.mkdir(parents=True, exist_ok=True)
        self._git(repository, "worktree", "add", "-b", branch_ref, str(worktree_path), base_commit)
        return PreparedWorktree(
            repository_path=str(repository),
            worktree_path=str(worktree_path),
            branch_ref=branch_ref,
            base_commit=base_commit,
        )

    def verify_candidate(
        self,
        *,
        repository_path: str,
        worktree_path: str,
        expected_branch: str,
        expected_base_commit: str,
        candidate_commit: str,
        submitted_base_commit: str,
    ) -> tuple[str, str]:
        repository = Path(self.validate_repository(repository_path))
        worktree = self._existing_directory(worktree_path, "worktree")
        if not worktree.is_relative_to(self.worktree_root):
            raise ValidationError("Assigned worktree is outside the configured HoH worktree root")
        actual_worktree = Path(
            self._git(worktree, "rev-parse", "--show-toplevel").stdout.strip()
        ).resolve()
        if actual_worktree != worktree:
            raise ValidationError("Assigned worktree is no longer a Git worktree root")
        active_branch = self._git(worktree, "branch", "--show-current").stdout.strip()
        if active_branch != expected_branch:
            raise ValidationError("Candidate must be submitted from its assigned worktree branch")

        canonical_base = self._resolve_commit(repository, submitted_base_commit)
        if canonical_base != expected_base_commit:
            raise ValidationError("Submitted base commit does not match the assigned worktree")
        canonical_candidate = self._resolve_commit(repository, candidate_commit)
        branch_commit = self._resolve_commit(repository, f"refs/heads/{expected_branch}")
        worktree_head = self._resolve_commit(worktree, "HEAD")
        if branch_commit != canonical_candidate or worktree_head != canonical_candidate:
            raise ValidationError("Candidate commit must be the current commit on its assigned branch")
        ancestry = self._git(
            repository,
            "merge-base",
            "--is-ancestor",
            expected_base_commit,
            canonical_candidate,
            check=False,
        )
        if ancestry.returncode != 0:
            raise ValidationError("Candidate commit does not descend from the assigned base commit")
        return canonical_candidate, canonical_base

    def verify_frozen_candidate(self, worktree_path: str, candidate_commit: str) -> str:
        """Ensure QA executes against a clean worktree still at the frozen candidate SHA."""
        worktree = self._existing_directory(worktree_path, "worktree")
        if not worktree.is_relative_to(self.worktree_root):
            raise ValidationError("Assigned worktree is outside the configured HoH worktree root")
        actual_worktree = Path(
            self._git(worktree, "rev-parse", "--show-toplevel").stdout.strip()
        ).resolve()
        if actual_worktree != worktree:
            raise ValidationError("Assigned worktree is no longer a Git worktree root")
        head = self._resolve_commit(worktree, "HEAD")
        if head != candidate_commit:
            raise ValidationError("Assigned worktree no longer matches the frozen candidate")
        status = self._git(worktree, "status", "--porcelain").stdout.strip()
        if status:
            raise ValidationError("Assigned worktree must be clean before QA runs project tests")
        return str(worktree)

    def changed_paths(
        self, *, repository_path: str, base_commit: str, candidate_commit: str
    ) -> list[str]:
        """Return deterministic paths changed between the immutable base and candidate."""
        repository = Path(self.validate_repository(repository_path))
        base = self._resolve_commit(repository, base_commit)
        candidate = self._resolve_commit(repository, candidate_commit)
        result = self._git(
            repository,
            "diff",
            "--name-only",
            "--no-renames",
            base,
            candidate,
        )
        return sorted({line.strip().replace("\\", "/") for line in result.stdout.splitlines() if line.strip()})

    def _worktree_path(self, project_id: str, ticket_id: str, agent_id: str) -> Path:
        segments = [self._safe_segment(value, field) for value, field in (
            (project_id, "project_id"),
            (ticket_id, "ticket_id"),
            (agent_id, "agent_id"),
        )]
        path = (self.worktree_root / segments[0] / segments[1] / segments[2]).resolve()
        if not path.is_relative_to(self.worktree_root):
            raise ValidationError("Worktree path is invalid")
        return path

    @staticmethod
    def _branch_ref(project_id: str, ticket_id: str, agent_id: str) -> str:
        segments = [GitWorktreeManager._safe_segment(value, field) for value, field in (
            (project_id, "project_id"),
            (ticket_id, "ticket_id"),
            (agent_id, "agent_id"),
        )]
        return f"hoh/{'/'.join(segments)}"

    @staticmethod
    def _safe_segment(value: str, field: str) -> str:
        if not _SAFE_SEGMENT_RE.fullmatch(value):
            raise ValidationError(f"{field} cannot be used in a Git worktree path")
        return value

    def _resolve_commit(self, repository: Path, revision: str) -> str:
        result = self._git(repository, "rev-parse", "--verify", f"{revision}^{{commit}}", check=False)
        if result.returncode != 0 or not result.stdout.strip():
            raise ValidationError("Git commit or branch cannot be resolved")
        return result.stdout.strip()

    @staticmethod
    def _existing_directory(value: str, field: str) -> Path:
        try:
            path = Path(value).expanduser().resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ValidationError(f"{field} does not exist") from exc
        if not path.is_dir():
            raise ValidationError(f"{field} must be a directory")
        return path

    @staticmethod
    def _git(path: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        try:
            completed = subprocess.run(
                ["git", "-C", str(path), *arguments],
                check=False,
                capture_output=True,
                text=True,
            )
        except OSError as exc:
            raise ValidationError("Git executable is unavailable") from exc
        if check and completed.returncode != 0:
            raise ValidationError("Git operation failed for the configured project repository")
        return completed
