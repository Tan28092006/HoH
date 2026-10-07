"""Mark a worker phase that the supervisor must not cancel or time out.

A worker moving the shared checkout's branch and working tree must finish that
move once it has started (R6/G14): killing it halfway leaves the ref at one
commit and the tree at another. The worker announces the phase with a file
under the artifact root, which the supervisor reads cheaply on every
cancellation check. The file names only the job; it carries no credentials.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path


def marker_path(artifact_root: Path, kind: str, job_id: str) -> Path:
    key = hashlib.sha256(f"{kind}:{job_id}".encode()).hexdigest()
    return Path(artifact_root) / "uncancellable" / f"{key}.marker"


def is_uncancellable(artifact_root: Path, kind: str, job_id: str) -> bool:
    try:
        return marker_path(artifact_root, kind, job_id).exists()
    except OSError:
        return False


def clear(artifact_root: Path, kind: str, job_id: str) -> None:
    """Drop a marker a dead worker left, so the next run of the job is cancellable."""
    try:
        marker_path(artifact_root, kind, job_id).unlink(missing_ok=True)
    except OSError:
        pass


@contextmanager
def uncancellable(artifact_root: Path, kind: str, job_id: str) -> Iterator[None]:
    path = marker_path(artifact_root, kind, job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(os.getpid()), encoding="utf-8")
    try:
        yield
    finally:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
