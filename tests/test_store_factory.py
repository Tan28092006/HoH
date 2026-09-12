"""Every process picks the same backing store, or they coordinate through none."""
from __future__ import annotations

from pathlib import Path

import pytest

from hoh_control_plane.store import SQLiteStore
from hoh_control_plane.store_factory import (
    ARTIFACT_ROOT_VARIABLE,
    DATABASE_PATH_VARIABLE,
    DATABASE_URL_VARIABLE,
    store_from_environment,
)


def test_a_local_install_still_gets_a_file(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv(DATABASE_URL_VARIABLE, raising=False)

    store = store_from_environment(tmp_path / "control-plane.db")

    assert isinstance(store, SQLiteStore)
    assert (tmp_path / "control-plane.db").exists(), "initialized, not just constructed"


def test_the_path_variable_is_honoured_when_no_argument_is_given(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv(DATABASE_URL_VARIABLE, raising=False)
    monkeypatch.setenv(DATABASE_PATH_VARIABLE, str(tmp_path / "from-env.db"))

    store = store_from_environment()

    assert Path(store.path) == tmp_path / "from-env.db"


def test_a_url_chooses_the_server_backend(monkeypatch) -> None:
    """Without this, the PostgreSQL backend had no way to be reached at all."""
    pytest.importorskip("psycopg", reason="PostgreSQL driver is not installed")
    monkeypatch.setenv(DATABASE_URL_VARIABLE, "postgresql://u:p@db.example.com/hoh")
    monkeypatch.setenv(ARTIFACT_ROOT_VARIABLE, "/srv/hoh")
    from hoh_control_plane.postgres_store import PostgresStore

    # Constructed without connecting: initialize would need a live server, and
    # what this asserts is the choice, not the connection.
    monkeypatch.setattr(PostgresStore, "initialize", lambda self: None)
    store = store_from_environment("data/ignored.db")

    assert isinstance(store, PostgresStore)
    assert store.artifact_root == Path("/srv/hoh"), "artifacts still need a place on disk"


def test_a_url_wins_over_a_path(monkeypatch, tmp_path: Path) -> None:
    """Falling back to a file would look like an empty board rather than an error."""
    pytest.importorskip("psycopg", reason="PostgreSQL driver is not installed")
    monkeypatch.setenv(DATABASE_URL_VARIABLE, "postgresql://u:p@db.example.com/hoh")
    monkeypatch.setenv(DATABASE_PATH_VARIABLE, str(tmp_path / "local.db"))
    from hoh_control_plane.postgres_store import PostgresStore

    monkeypatch.setattr(PostgresStore, "initialize", lambda self: None)

    assert isinstance(store_from_environment(), PostgresStore)
    assert not (tmp_path / "local.db").exists()


def test_an_unrecognised_url_is_refused_rather_than_ignored(monkeypatch) -> None:
    monkeypatch.setenv(DATABASE_URL_VARIABLE, "mysql://u:p@db.example.com/hoh")

    with pytest.raises(SystemExit) as error:
        store_from_environment()

    assert DATABASE_PATH_VARIABLE in str(error.value), "it says what to use instead"


def test_a_blank_url_is_not_a_url(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(DATABASE_URL_VARIABLE, "   ")

    assert isinstance(store_from_environment(tmp_path / "x.db"), SQLiteStore)
