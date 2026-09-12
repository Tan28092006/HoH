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


def store_from_environment(database_path: str | Path | None = None) -> Any:
    """The store this process should use, initialized and ready.

    HOH_DATABASE_URL wins when set: a deployment that has moved to PostgreSQL
    must not silently fall back to a local file when a path is also configured,
    because that failure looks like an empty board rather than an error.
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

        path = database_path or os.getenv(DATABASE_PATH_VARIABLE, "data/control-plane.db")
        store = SQLiteStore(Path(path))
    store.initialize()
    return store
