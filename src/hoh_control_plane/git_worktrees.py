from __future__ import annotations

import hashlib
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from hoh_control_plane.domain import ValidationError


_SAFE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_GIT_COMMAND_TIMEOUT_SECONDS = 15

CLEAN_CHECKPOINT = "clean_checkpoint"
DIRTY = "dirty"
MISSING = "missing"
UNVERIFIED = "unverified"
BASE_DRIFT = "base_drift"


@dataclass(frozen=True, slots=True)
class PreparedWorktree:
    repository_path: str
    worktree_path: str
    branch_ref: str
    base_commit: str


@dataclass(frozen=True, slots=True)
class WorktreeCheckpoint:
    """Read-only verdict about an abandoned worktree a replacement may inherit."""

    status: str
    head_commit: str | None
    detail: str
    untracked_paths: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RecoveryArtifact:
    path: str
    sha256: str
    byte_size: int


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
        start_commit: str | None = None,
    ) -> PreparedWorktree:
        repository = Path(self.validate_repository(repository_path))
        branch_ref = self._branch_ref(project_id, ticket_id, agent_id)
        base_commit = self._resolve_commit(repository, f"refs/heads/{default_branch}")
        initial_commit = (
            self._resolve_commit(repository, start_commit) if start_commit else base_commit
        )
        ancestry = self._git(
            repository,
            "merge-base",
            "--is-ancestor",
            base_commit,
            initial_commit,
            check=False,
        )
        if ancestry.returncode != 0:
            raise ValidationError("Worktree start commit does not descend from the default branch")
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
        self._git(repository, "worktree", "add", "-b", branch_ref, str(worktree_path), initial_commit)
        return PreparedWorktree(
            repository_path=str(repository),
            worktree_path=str(worktree_path),
            branch_ref=branch_ref,
            base_commit=base_commit,
        )

    def commit_descends_from(
        self,
        *,
        repository_path: str,
        ancestor_commit: str,
        descendant_commit: str,
    ) -> bool:
        """Return whether ``descendant_commit`` can safely be based on ``ancestor_commit``."""
        repository = Path(self.validate_repository(repository_path))
        ancestor = self._resolve_commit(repository, ancestor_commit)
        descendant = self._resolve_commit(repository, descendant_commit)
        result = self._git(
            repository,
            "merge-base",
            "--is-ancestor",
            ancestor,
            descendant,
            check=False,
        )
        if result.returncode not in {0, 1}:
            raise ValidationError("Unable to determine Git commit ancestry")
        return result.returncode == 0

    def inspect_checkpoint(
        self,
        *,
        repository_path: str,
        worktree_path: str,
        expected_branch: str,
        expected_base_commit: str,
        default_branch: str,
    ) -> WorktreeCheckpoint:
        """Classify an abandoned worktree without mutating it or trusting its contents.

        Every unhappy path returns a verdict instead of raising so a takeover can
        fail closed on code transfer and still record why.
        """
        try:
            worktree = self._existing_directory(worktree_path, "worktree")
        except ValidationError:
            return WorktreeCheckpoint(
                MISSING, None, "The prior worktree directory is missing."
            )
        if not worktree.is_relative_to(self.worktree_root):
            return WorktreeCheckpoint(
                UNVERIFIED, None, "The prior worktree is outside the HoH worktree root."
            )
        try:
            repository = Path(self.validate_repository(repository_path))
            toplevel = self._git(worktree, "rev-parse", "--show-toplevel", check=False)
            if toplevel.returncode != 0 or Path(toplevel.stdout.strip()).resolve() != worktree:
                return WorktreeCheckpoint(
                    UNVERIFIED, None, "The prior worktree is no longer a Git worktree root."
                )
            active_branch = self._git(worktree, "branch", "--show-current", check=False)
            if active_branch.stdout.strip() != expected_branch:
                return WorktreeCheckpoint(
                    UNVERIFIED, None, "The prior worktree left its assigned branch."
                )
            head = self._resolve_commit(worktree, "HEAD")
            # --untracked-files=all names each abandoned file instead of collapsing
            # a whole directory to one entry, so nothing is silently lost.
            status = self._git(
                worktree, "status", "--porcelain", "--untracked-files=all", check=False
            )
            if status.returncode != 0:
                return WorktreeCheckpoint(
                    UNVERIFIED, head, "The prior worktree status could not be read."
                )
            if status.stdout.strip():
                return WorktreeCheckpoint(
                    DIRTY,
                    head,
                    "The prior worktree holds uncommitted changes.",
                    self._untracked_paths(status.stdout),
                )
            if not self._is_ancestor(repository, expected_base_commit, head):
                return WorktreeCheckpoint(
                    UNVERIFIED,
                    head,
                    "The prior branch head does not descend from its recorded base commit.",
                )
            if not self._is_ancestor(repository, f"refs/heads/{default_branch}", head):
                return WorktreeCheckpoint(
                    BASE_DRIFT,
                    head,
                    "The prior checkpoint no longer descends from the default branch.",
                )
        except ValidationError as error:
            return WorktreeCheckpoint(
                UNVERIFIED, None, f"The prior worktree could not be verified: {error}"
            )
        return WorktreeCheckpoint(
            CLEAN_CHECKPOINT, head, "The prior worktree is clean at a verified checkpoint."
        )

    def capture_recovery_patch(
        self, *, worktree_path: str, destination: Path
    ) -> RecoveryArtifact | None:
        """Persist a private checksummed patch of tracked uncommitted work.

        The old worktree is only read: nothing is staged, committed or merged, and
        the payload never reaches the database or an activity log.
        """
        try:
            worktree = self._existing_directory(worktree_path, "worktree")
        except ValidationError:
            return None
        patch = self._git(worktree, "diff", "HEAD", "--binary", check=False)
        if patch.returncode != 0 or not patch.stdout:
            return None
        payload = patch.stdout.encode("utf-8")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)
        return RecoveryArtifact(
            path=str(destination),
            sha256=hashlib.sha256(payload).hexdigest(),
            byte_size=len(payload),
        )

    def _is_ancestor(self, repository: Path, ancestor: str, descendant: str) -> bool:
        result = self._git(
            repository,
            "merge-base",
            "--is-ancestor",
            self._resolve_commit(repository, ancestor),
            descendant,
            check=False,
        )
        if result.returncode not in {0, 1}:
            raise ValidationError("Unable to determine Git commit ancestry")
        return result.returncode == 0

    @staticmethod
    def _untracked_paths(status_output: str) -> tuple[str, ...]:
        paths = {
            line[3:].strip().strip('"').replace("\\", "/")
            for line in status_output.splitlines()
            if line.startswith("?? ")
        }
        return tuple(sorted(paths))

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

    def fast_forward_default_branch(
        self,
        *,
        repository_path: str,
        default_branch: str,
        expected_base_commit: str,
        candidate_commit: str,
    ) -> tuple[bool, str]:
        """Fast-forward the checked-out default branch only when its base is unchanged."""
        repository = Path(self.validate_repository(repository_path))
        active_branch = self._git(repository, "branch", "--show-current").stdout.strip()
        if active_branch != default_branch:
            raise ValidationError("Repository default branch is not checked out for integration")
        current = self._resolve_commit(repository, f"refs/heads/{default_branch}")
        expected_base = self._resolve_commit(repository, expected_base_commit)
        candidate = self._resolve_commit(repository, candidate_commit)
        if current != expected_base:
            return False, current
        merged = self._git(repository, "merge", "--ff-only", candidate, check=False)
        if merged.returncode != 0:
            raise ValidationError("Candidate cannot fast-forward the default branch")
        return True, candidate

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
                timeout=_GIT_COMMAND_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as exc:
            raise ValidationError("Git operation timed out") from exc
        except OSError as exc:
            raise ValidationError("Git executable is unavailable") from exc
        if check and completed.returncode != 0:
            raise ValidationError("Git operation failed for the configured project repository")
        return completed
