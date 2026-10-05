from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from hoh_control_plane.domain import ValidationError
from hoh_control_plane.resource_lock import exclusive_file_lock
from hoh_control_plane.managed_process import run_managed


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
        # A leading dot cannot appear in a project_id, so integration checkouts can
        # never collide with a Developer worktree path.
        self.integration_root = self.worktree_root / ".integration"

    def validate_repository(self, repository_path: str) -> str:
        path = self._existing_directory(repository_path, "repository_path")
        result = self._git(path, "rev-parse", "--show-toplevel", check=False)
        if result.returncode != 0 or not result.stdout.strip():
            raise ValidationError("repository_path must reference a Git repository")
        return str(Path(result.stdout.strip()).resolve())

    def sync_upstream(self, repository_path: str, default_branch: str) -> dict[str, object]:
        """Fetch the configured upstream and return a bounded, read-only change manifest.

        This deliberately never pulls, merges, rebases or checks out a branch. The
        control plane first needs facts about remote movement; changing the
        integration checkout while worktrees are active would destroy those facts.
        """
        repository = Path(self.validate_repository(repository_path))
        local_ref = f"refs/heads/{default_branch}"
        remote = self._git(
            repository, "config", "--get", f"branch.{default_branch}.remote", check=False
        ).stdout.strip()
        if not remote:
            return {
                "relation": "no_upstream",
                "local_commit": self._resolve_commit(repository, local_ref),
                "upstream_ref": None,
                "upstream_commit": None,
                "ahead": 0,
                "behind": 0,
                "incoming_commits": [],
                "incoming_changes": [],
                "local_changes": [],
                "remote_branches": {},
            }
        if remote != ".":
            configured_remotes = {
                line.strip()
                for line in self._git(repository, "remote").stdout.splitlines()
                if line.strip()
            }
            if remote not in configured_remotes:
                raise ValidationError(
                    "The default branch names a Git remote that is not configured",
                    {"reason": "upstream_remote_missing", "remote": remote},
                )
            fetched = self._git(
                repository,
                "fetch",
                "--prune",
                "--no-tags",
                "--",
                remote,
                f"+refs/heads/*:refs/remotes/{remote}/*",
                check=False,
            )
            if fetched.returncode != 0:
                raise ValidationError(
                    "Could not fetch the configured Git upstream; repository freshness is unknown",
                    {"reason": "upstream_fetch_failed", "remote": remote},
                )
        # Read the local branch only after the fetch: a merge that lands while
        # the fetch runs must be observed, not overwritten by a stale SHA.
        local_commit = self._resolve_commit(repository, local_ref)
        remote_branches: dict[str, str] = {}
        if remote != ".":
            refs = self._git(
                repository,
                "for-each-ref",
                "--format=%(refname:short)%09%(objectname)",
                f"refs/remotes/{remote}/",
            ).stdout
            for line in refs.splitlines()[:200]:
                name, separator, commit = line.partition("\t")
                if separator and name != f"{remote}/HEAD":
                    remote_branches[name] = commit
        upstream_name = self._git(
            repository,
            "rev-parse",
            "--abbrev-ref",
            f"{default_branch}@{{upstream}}",
            check=False,
        ).stdout.strip()
        if not upstream_name:
            return {
                "relation": "no_upstream",
                "local_commit": local_commit,
                "upstream_ref": None,
                "upstream_commit": None,
                "ahead": 0,
                "behind": 0,
                "incoming_commits": [],
                "incoming_changes": [],
                "local_changes": [],
                "remote_branches": remote_branches,
            }
        upstream_commit = self._resolve_commit(repository, upstream_name)
        counts = self._git(
            repository, "rev-list", "--left-right", "--count",
            f"{local_ref}...{upstream_name}",
        ).stdout.split()
        ahead, behind = (int(counts[0]), int(counts[1]))
        relation = (
            "up_to_date" if ahead == 0 and behind == 0 else
            "behind" if ahead == 0 else
            "ahead" if behind == 0 else
            "diverged"
        )
        merge_base = self._git(
            repository, "merge-base", local_ref, upstream_name
        ).stdout.strip()

        def changes(end: str) -> list[dict[str, str]]:
            output = self._git(
                repository, "diff", "--name-status", "--no-renames", merge_base, end
            ).stdout
            rows: list[dict[str, str]] = []
            for line in output.splitlines()[:200]:
                status, separator, path = line.partition("\t")
                if separator and path:
                    rows.append({"status": status, "path": path})
            return rows

        incoming_log = self._git(
            repository, "log", "--format=%H%x09%s", "--max-count=100",
            f"{merge_base}..{upstream_name}",
        ).stdout
        incoming_commits = []
        for line in incoming_log.splitlines():
            commit, separator, subject = line.partition("\t")
            if separator:
                incoming_commits.append({"commit": commit, "subject": subject[:500]})
        return {
            "relation": relation,
            "local_commit": local_commit,
            "upstream_ref": upstream_name,
            "upstream_commit": upstream_commit,
            "ahead": ahead,
            "behind": behind,
            "incoming_commits": incoming_commits,
            "incoming_changes": changes(upstream_name),
            "local_changes": changes(local_ref),
            "remote_branches": remote_branches,
        }

    def remote_commit_delta(
        self,
        repository_path: str,
        previous_commit: str | None,
        commit: str | None,
        *,
        limit: int = 100,
        baseline_commit: str | None = None,
    ) -> dict[str, object]:
        """Describe commits introduced by one observed remote-head movement.

        A fast-forward can contain several commits when the local observer was
        asleep.  Returning each one preserves the paper's commit-as-iteration
        boundary even though the Planner receives them in one wakeup.  New,
        deleted and rewritten branches stay bounded to their head identity; a
        new branch's entire historical ancestry is not new project work.
        """
        repository = Path(self.validate_repository(repository_path))
        if commit is None:
            return {"relation": "deleted", "commits": [], "changes": []}
        resolved = self._resolve_commit(repository, commit)

        def changes(start: str | None, end: str) -> list[dict[str, str]]:
            if start is None:
                output = self._git(
                    repository, "show", "--format=", "--name-status", "--no-renames", end
                ).stdout
            else:
                output = self._git(
                    repository, "diff", "--name-status", "--no-renames", start, end
                ).stdout
            result = []
            for line in output.splitlines()[:200]:
                status, separator, path = line.partition("\t")
                if separator and path:
                    result.append({"status": status, "path": path})
            return result

        if previous_commit is None:
            if baseline_commit is not None:
                baseline = self._resolve_commit(repository, baseline_commit)
                merge_base = self._git(
                    repository, "merge-base", baseline, resolved, check=False
                ).stdout.strip()
                if merge_base:
                    output = self._git(
                        repository,
                        "log",
                        "--reverse",
                        "--format=%H%x09%s",
                        f"--max-count={limit}",
                        f"{merge_base}..{resolved}",
                    ).stdout
                    commits = []
                    for line in output.splitlines():
                        sha, separator, subject = line.partition("\t")
                        if separator:
                            commits.append({"commit": sha, "subject": subject[:500]})
                    return {
                        "relation": "new",
                        "commits": commits,
                        "changes": changes(merge_base, resolved),
                    }
            subject = self._git(
                repository, "show", "-s", "--format=%s", resolved
            ).stdout.strip()
            return {
                "relation": "new",
                "commits": [{"commit": resolved, "subject": subject[:500]}],
                "changes": changes(None, resolved),
            }
        previous = self._resolve_commit(repository, previous_commit)
        ancestry = self._git(
            repository, "merge-base", "--is-ancestor", previous, resolved, check=False
        )
        if ancestry.returncode == 0:
            output = self._git(
                repository,
                "log",
                "--reverse",
                "--format=%H%x09%s",
                f"--max-count={limit}",
                f"{previous}..{resolved}",
            ).stdout
            commits = []
            for line in output.splitlines():
                sha, separator, subject = line.partition("\t")
                if separator:
                    commits.append({"commit": sha, "subject": subject[:500]})
            return {
                "relation": "fast_forward",
                "commits": commits,
                "changes": changes(previous, resolved),
            }
        if ancestry.returncode not in {0, 1}:
            raise ValidationError("Unable to classify remote branch movement")
        subject = self._git(
            repository, "show", "-s", "--format=%s", resolved
        ).stdout.strip()
        merge_base = self._git(
            repository, "merge-base", previous, resolved, check=False
        ).stdout.strip()
        return {
            "relation": "rewritten",
            "commits": [{"commit": resolved, "subject": subject[:500]}],
            "changes": changes(merge_base or None, resolved),
        }

    def prepare(
        self,
        *,
        repository_path: str,
        default_branch: str,
        project_id: str,
        ticket_id: str,
        agent_id: str,
        start_commit: str | None = None,
        generation: str | None = None,
    ) -> PreparedWorktree:
        repository = Path(self.validate_repository(repository_path))
        branch_ref = self._branch_ref(project_id, ticket_id, agent_id)
        if generation is not None:
            branch_ref += "/" + self._safe_segment(generation, "generation")
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
        if generation is not None:
            worktree_path = (worktree_path / generation).resolve()
            if not worktree_path.is_relative_to(self.worktree_root):
                raise ValidationError("Worktree path is invalid")
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
        expected_iteration_parent: str | None = None,
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
        allowed_parents = {canonical_base}
        if expected_iteration_parent:
            allowed_parents.add(self._resolve_commit(repository, expected_iteration_parent))
        self._require_single_commit_iteration(repository, allowed_parents, canonical_candidate)
        return canonical_candidate, canonical_base

    def verify_pushed_candidate(
        self,
        *,
        repository_path: str,
        candidate_commit: str,
        submitted_base_commit: str,
        expected_iteration_parent: str | None = None,
    ) -> tuple[str, str]:
        """Verify a candidate the service never had a worktree for.

        Under the agent-workspace model the Developer holds its own checkout, so
        there is no service-side tree to inspect. Every fact is therefore derived
        from the repository the service itself can read: if the agent has not made
        the commit reachable there, the claim cannot be checked and is refused
        rather than taken on the agent's word.
        """
        repository = Path(self.validate_repository(repository_path))
        try:
            canonical_candidate = self._resolve_commit(repository, candidate_commit)
            canonical_base = self._resolve_commit(repository, submitted_base_commit)
        except ValidationError as error:
            raise ValidationError(
                "The service cannot resolve this candidate in the project repository. "
                "Push the branch so the commit is reachable before submitting."
            ) from error
        ancestry = self._git(
            repository,
            "merge-base",
            "--is-ancestor",
            canonical_base,
            canonical_candidate,
            check=False,
        )
        if ancestry.returncode != 0:
            raise ValidationError("Candidate commit does not descend from the submitted base commit")
        allowed_parents = {canonical_base}
        if expected_iteration_parent:
            allowed_parents.add(self._resolve_commit(repository, expected_iteration_parent))
        self._require_single_commit_iteration(repository, allowed_parents, canonical_candidate)
        return canonical_candidate, canonical_base

    def _require_single_commit_iteration(
        self, repository: Path, allowed_parent_commits: set[str], candidate_commit: str
    ) -> None:
        """Require one ordinary commit on the current iteration artifact.

        The candidate is the artifact emitted by one Developer iteration.  A
        multi-commit range would hide intermediate iteration boundaries from
        Planner; a merge commit would import an unreviewed side history. A
        rework iteration may continue the previously assessed candidate or
        restart from the newly frozen base after mainline drift.
        """
        parents = self._git(
            repository, "rev-list", "--parents", "-n", "1", candidate_commit
        ).stdout.strip().split()
        if (
            len(parents) != 2
            or parents[0] != candidate_commit
            or parents[1] not in allowed_parent_commits
        ):
            raise ValidationError(
                "One Developer iteration must submit exactly one non-merge commit "
                "whose parent is the frozen base or the previously assessed candidate"
            )

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

    @contextmanager
    def qa_checkout(self, *, repository_path: str, test_run_id: str, candidate_commit: str) -> Iterator[str]:
        """Run the frozen SHA with private objects, refs and configuration."""
        repository = Path(self.validate_repository(repository_path))
        candidate = self._resolve_commit(repository, candidate_commit)
        path = self.qa_path(test_run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        # A previous run is never force-removed on entry. A crash needs explicit
        # recovery, and a second worker must not delete a live browser/test tree.
        with self._snapshot_checkout(repository, path, candidate) as snapshot:
            yield snapshot

    @contextmanager
    def _snapshot_checkout(self, repository: Path, path: Path, candidate: str) -> Iterator[str]:
        if path.exists() or path.is_symlink():
            raise ValidationError('Existing test checkout requires explicit recovery')
        path.mkdir(parents=True)
        try:
            self._git(path, 'init', '--template=')
            # Fetch copies objects. There is no shared .git, alternate object
            # directory, remote configuration or hardlink back to production.
            self._git(path, '-c', 'protocol.file.allow=always', 'fetch', '--no-tags', str(repository), candidate)
            self._git(path, '-c', 'core.hooksPath=', 'checkout', '--detach', candidate)
            yield self.verify_frozen_candidate(str(path), candidate)
        finally:
            if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()):
                raise ValidationError('Test checkout became a filesystem link; retained for recovery')
            if not path.resolve().is_relative_to(self.worktree_root):
                raise ValidationError('Test checkout escaped its managed root')
            if path.exists():
                def remove_readonly(function, filename, error):
                    item = Path(filename)
                    if not isinstance(error, PermissionError) or not item.resolve().is_relative_to(path.resolve()) or item.is_symlink():
                        raise error
                    os.chmod(item, 0o700)
                    function(item)
                shutil.rmtree(path, onexc=remove_readonly)

    def repository_lock(self, repository_path: str):
        repository = Path(self.validate_repository(repository_path))
        common = Path(self._git(repository, 'rev-parse', '--git-common-dir').stdout.strip())
        if not common.is_absolute():
            common = repository / common
        return exclusive_file_lock(common.resolve() / 'hoh-integration.lock')

    def qa_path(self, test_run_id: str) -> Path:
        path = (self.worktree_root / ".qa" / self._safe_segment(test_run_id, "test_run_id")).resolve()
        if not path.is_relative_to(self.worktree_root):
            raise ValidationError("QA worktree path is invalid")
        return path

    @contextmanager
    def integration_checkout(
        self,
        *,
        repository_path: str,
        project_id: str,
        ticket_id: str,
        candidate_commit: str,
    ) -> Iterator[str]:
        """Materialize the exact candidate on a disposable detached integration worktree."""
        repository = Path(self.validate_repository(repository_path))
        candidate = self._resolve_commit(repository, candidate_commit)
        checkout_path = self._integration_path(project_id, ticket_id) / uuid4().hex
        checkout_path.parent.mkdir(parents=True, exist_ok=True)
        with self._snapshot_checkout(repository, checkout_path, candidate) as snapshot:
            yield snapshot

    def _integration_path(self, project_id: str, ticket_id: str) -> Path:
        segments = [self._safe_segment(value, field) for value, field in (
            (project_id, "project_id"),
            (ticket_id, "ticket_id"),
        )]
        path = (self.integration_root / segments[0] / segments[1]).resolve()
        if not path.is_relative_to(self.integration_root):
            raise ValidationError("Integration worktree path is invalid")
        return path

    def _discard_integration_checkout(self, repository: Path, checkout_path: Path) -> None:
        """Leave no integration checkout behind, whether or not the gate ran."""
        if checkout_path.exists():
            removed = self._git(
                repository, "worktree", "remove", "--force", str(checkout_path), check=False
            )
            if removed.returncode != 0:
                self._git(repository, "worktree", "prune", check=False)
            if checkout_path.exists():
                shutil.rmtree(checkout_path, ignore_errors=True)

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
        if self._git(repository, 'status', '--porcelain').stdout.strip():
            raise ValidationError('Repository must be clean before integration')
        current = self._resolve_commit(repository, f"refs/heads/{default_branch}")
        expected_base = self._resolve_commit(repository, expected_base_commit)
        candidate = self._resolve_commit(repository, candidate_commit)
        if current != expected_base:
            return False, current
        merged = self._git(repository, "merge", "--ff-only", candidate, check=False)
        if merged.returncode != 0:
            raise ValidationError("Candidate cannot fast-forward the default branch")
        return True, candidate

    def commit_is_on_branch(
        self, *, repository_path: str, default_branch: str, commit: str
    ) -> bool:
        """Whether a commit is genuinely reachable from the default branch.

        Asked of Git rather than of the caller: a claim that something landed is
        exactly the kind of assertion that must be checked, not believed.
        """
        repository = Path(self.validate_repository(repository_path))
        try:
            resolved = self._resolve_commit(repository, commit)
        except ValidationError:
            return False
        return self._is_ancestor(repository, resolved, f"refs/heads/{default_branch}")

    def equivalent_landing(
        self,
        *,
        repository_path: str,
        default_branch: str,
        base_commit: str,
        candidate_commit: str,
        limit: int = 200,
    ) -> str | None:
        """The default-branch commit whose own diff equals the candidate's, or None.

        Work applied as a diff or cherry-picked lands under a different commit, so
        ancestry misses it. ``git diff base candidate`` is compared, by stable
        patch-id, with each recent non-merge commit's diff against its parent.
        Any Git failure answers None: not proven is not landed.
        """
        try:
            repository = Path(self.validate_repository(repository_path))
            base = self._resolve_commit(repository, base_commit)
            candidate = self._resolve_commit(repository, candidate_commit)
            wanted = self._patch_ids(
                repository,
                self._git(repository, "diff", "--no-color", "--no-ext-diff", base, candidate).stdout,
            )
            if not wanted:
                return None
            target = wanted[0][0]
            history = self._git(
                repository, "log", "-p", "--no-color", "--no-ext-diff", "--no-merges",
                f"-n{int(limit)}", "--format=commit %H",
                f"{base}..refs/heads/{default_branch}",
                check=False,
            )
            if history.returncode != 0:
                return None
            for patch_id, commit in self._patch_ids(repository, history.stdout):
                if patch_id == target and commit != candidate:
                    return commit
        except (ValidationError, OSError, subprocess.SubprocessError):
            return None
        return None

    @staticmethod
    def _patch_ids(repository: Path, patch_text: str) -> list[tuple[str, str]]:
        if not patch_text.strip():
            return []
        result = subprocess.run(
            ["git", "-C", str(repository), "patch-id", "--stable"],
            input=patch_text.encode("utf-8", errors="replace"),
            capture_output=True,
            timeout=_GIT_COMMAND_TIMEOUT_SECONDS * 2,
            check=False,
        )
        if result.returncode != 0:
            return []
        pairs = []
        for line in result.stdout.decode("utf-8", errors="replace").splitlines():
            parts = line.split()
            if len(parts) == 2:
                pairs.append((parts[0], parts[1]))
        return pairs

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

    def expand_commit(self, repository: Path, prefix: str, field: str) -> str:
        """Resolve an abbreviated commit SHA to the full one, or refuse it.

        Only a commit whose name starts with the prefix counts, so a branch or
        tag that happens to be spelled in hex can never stand in for it.
        """
        result = self._git(repository, "rev-parse", "--verify", "--quiet", f"{prefix}^{{commit}}", check=False)
        full = result.stdout.strip().lower()
        if result.returncode == 0 and full.startswith(prefix.lower()):
            return full
        matches = self._git(repository, "rev-parse", f"--disambiguate={prefix}", check=False)
        if len(matches.stdout.split()) > 1:
            raise ValidationError(
                f"{field} {prefix} is ambiguous in the project repository; pass more characters",
                {"reason": "commit_prefix_ambiguous", "field": field},
            )
        raise ValidationError(
            f"{field} {prefix} names no commit in the project repository",
            {"reason": "commit_unknown", "field": field},
        )

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
            command = ['git', '-C', str(path), *arguments]
            result = run_managed(command, cwd=path, timeout=_GIT_COMMAND_TIMEOUT_SECONDS, output_limit=2_000_000)
            if result.timed_out:
                raise ValidationError('Git operation timed out; its process tree was stopped')
            if any(output.endswith('\n[output truncated]') for output in (result.stdout, result.stderr)):
                raise ValidationError('Git output exceeded the capture limit; refusing incomplete evidence')
            completed = subprocess.CompletedProcess(command, result.returncode, result.stdout, result.stderr)
        except subprocess.TimeoutExpired as exc:
            raise ValidationError("Git operation timed out") from exc
        except OSError as exc:
            raise ValidationError("Git executable is unavailable") from exc
        if check and completed.returncode != 0:
            raise ValidationError("Git operation failed for the configured project repository")
        return completed
