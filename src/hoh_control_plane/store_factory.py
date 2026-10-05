"""Choosing the backing store from the environment.

The PostgreSQL backend existed for a while with no way to reach it: every entry
point built a SQLiteStore by hand, so the store seam that the port was for was
only ever exercised by tests. Deploying across machines needs a server-backed
store, and needs every process -- the MCP endpoint, the scheduler and each
worker -- to agree on which one, or they coordinate through different databases
and each is correct on its own.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any


DATABASE_URL_VARIABLE = "HOH_DATABASE_URL"
DATABASE_PATH_VARIABLE = "HOH_DATABASE_PATH"
ARTIFACT_ROOT_VARIABLE = "HOH_ARTIFACT_ROOT"

_POSTGRES_SCHEMES = ("postgresql://", "postgres://")


def database_path_from_environment(database_path: str | Path | None = None) -> Path | None:
    """The SQLite file this process should open, or None when a URL is configured.

    There is deliberately no default. A relative ``data/control-plane.db`` used
    to be the fallback, so a process started without the host's environment
    quietly created a stray, empty database under whatever directory it was
    launched from, which looks like an empty board rather than an error.
    """
    if database_path:
        return Path(database_path)
    configured = (os.getenv(DATABASE_PATH_VARIABLE) or "").strip()
    if configured:
        return Path(configured)
    if (os.getenv(DATABASE_URL_VARIABLE) or "").strip():
        return None
    raise SystemExit(
        f"{DATABASE_PATH_VARIABLE} is not set. Set it to the database this process "
        f"should use (or set {DATABASE_URL_VARIABLE} for PostgreSQL); HoH no longer "
        "falls back to a relative data/control-plane.db."
    )


def store_from_environment(
    database_path: str | Path | None = None, *, allow_migration: bool = False
) -> Any:
    """The store this process should use, initialized and ready.

    HOH_DATABASE_URL wins when set: a deployment that has moved to PostgreSQL
    must not silently fall back to a local file when a path is also configured,
    because that failure looks like an empty board rather than an error.

    Every serving process opens its store through here, so by default a
    database older than this build is refused rather than upgraded (see
    ``SQLiteStore.initialize``). ``hoh-ops migrate`` is the one caller that
    passes ``allow_migration=True``, after taking a backup.
    """
    url = (os.getenv(DATABASE_URL_VARIABLE) or "").strip()
    if url:
        if not url.startswith(_POSTGRES_SCHEMES):
            raise SystemExit(
                f"{DATABASE_URL_VARIABLE} must be a postgresql:// URL; "
                f"use {DATABASE_PATH_VARIABLE} for a local SQLite file"
            )
        from hoh_control_plane.postgres_store import PostgresStore

        # A server-backed store has no directory of its own, and worktrees,
        # evidence and recovery patches still have to live somewhere on disk.
        store = PostgresStore(
            url, artifact_root=Path(os.getenv(ARTIFACT_ROOT_VARIABLE, "data"))
        )
    else:
        from hoh_control_plane.store import SQLiteStore

        path = database_path_from_environment(database_path)
        assert path is not None  # no URL is set, so a path was found or this exited
        store = SQLiteStore(path)
    store.initialize(allow_migration=allow_migration)
    return store
