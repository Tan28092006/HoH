"""Serving processes refuse an older database; only `hoh-ops migrate` upgrades it.

A newer checkout started against the real database used to upgrade it in place,
with no backup, and the installed older service then refused to start. Every
test here uses a throwaway file under tmp_path.
"""
from __future__ import annotations

import hashlib
import sqlite3
import sys
from pathlib import Path

import pytest

from hoh_control_plane import operations, scheduler
from hoh_control_plane.api import create_app
from hoh_control_plane.store import (
    SCHEMA_VERSION,
    SQLiteStore,
    SchemaUpgradeRequired,
    SchemaVersionError,
)
from hoh_control_plane.store_factory import (
    DATABASE_PATH_VARIABLE,
    DATABASE_URL_VARIABLE,
    database_path_from_environment,
    store_from_environment,
)


def _version(path: Path) -> int | None:
    connection = sqlite3.connect(path)
    try:
        row = connection.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    finally:
        connection.close()
    return None if row is None else int(row[0])


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _older_database(path: Path, version: int = SCHEMA_VERSION - 1) -> Path:
    SQLiteStore(path).initialize()
    connection = sqlite3.connect(path)
    try:
        if version == 0:
            connection.execute("DROP TABLE schema_meta")
        else:
            connection.execute("UPDATE schema_meta SET value = ?", (str(version),))
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        connection.close()
    return path


@pytest.fixture(autouse=True)
def _no_inherited_database(monkeypatch):
    monkeypatch.delenv(DATABASE_PATH_VARIABLE, raising=False)
    monkeypatch.delenv(DATABASE_URL_VARIABLE, raising=False)


@pytest.mark.parametrize("version", [0, SCHEMA_VERSION - 1])
def test_a_serving_store_refuses_an_older_database_without_changing_it(tmp_path, version) -> None:
    path = _older_database(tmp_path / "older.db", version)
    before = _digest(path)

    with pytest.raises(SchemaUpgradeRequired) as raised:
        store_from_environment(path)

    message = str(raised.value)
    assert f"schema version {version}" in message
    assert "hoh-ops migrate" in message
    assert "Nothing was changed" in message
    assert raised.value.code == "schema_upgrade_required"
    assert isinstance(raised.value, SchemaVersionError)
    assert _digest(path) == before, "the file is byte-for-byte untouched"
    assert _version(path) == (None if version == 0 else version)


def test_the_store_refuses_when_told_not_to_migrate(tmp_path) -> None:
    path = _older_database(tmp_path / "older.db")
    before = _digest(path)

    with pytest.raises(SchemaUpgradeRequired):
        SQLiteStore(path).initialize(allow_migration=False)

    assert _digest(path) == before


def test_a_new_database_is_still_created_by_a_serving_process(tmp_path) -> None:
    path = tmp_path / "fresh" / "control-plane.db"

    store_from_environment(path)

    assert _version(path) == SCHEMA_VERSION


def test_a_current_database_opens_normally(tmp_path) -> None:
    path = tmp_path / "current.db"
    SQLiteStore(path).initialize()

    store_from_environment(path)
    store_from_environment(path)

    assert _version(path) == SCHEMA_VERSION


def test_the_rest_api_refuses_an_older_database(tmp_path) -> None:
    path = _older_database(tmp_path / "older.db")
    before = _digest(path)

    with pytest.raises(SchemaUpgradeRequired):
        create_app(path, local_mode=True)

    assert _digest(path) == before


def test_migrate_backs_up_first_then_upgrades(tmp_path) -> None:
    path = _older_database(tmp_path / "control-plane.db")
    backups = tmp_path / "backups"

    result = operations.migrate_database(path, backups)

    assert result["migrated"] is True
    assert result["from_schema_version"] == SCHEMA_VERSION - 1
    assert result["schema_version"] == SCHEMA_VERSION
    backup = Path(result["backup"])
    assert backup.parent == backups
    assert _version(backup) == SCHEMA_VERSION - 1, "the copy is of the database before the upgrade"
    assert _version(path) == SCHEMA_VERSION
    store_from_environment(path)  # and serving processes now accept it


def test_migrate_is_a_no_op_on_a_current_database(tmp_path) -> None:
    path = tmp_path / "control-plane.db"
    SQLiteStore(path).initialize()

    result = operations.migrate_database(path)

    assert result == {
        "migrated": False, "database": str(path), "schema_version": SCHEMA_VERSION, "backup": None,
    }
    assert sorted(p.name for p in tmp_path.glob("*.db")) == ["control-plane.db"]


def test_migrate_refuses_a_missing_database(tmp_path) -> None:
    with pytest.raises(SystemExit, match="nothing to migrate"):
        operations.migrate_database(tmp_path / "absent.db")
    assert not (tmp_path / "absent.db").exists()


def test_migrate_refuses_a_newer_database_untouched(tmp_path) -> None:
    path = _older_database(tmp_path / "newer.db", SCHEMA_VERSION + 1)
    before = _digest(path)

    with pytest.raises(SchemaVersionError, match="newer"):
        operations.migrate_database(path)

    assert _digest(path) == before
    assert sorted(p.name for p in tmp_path.glob("*.db")) == ["newer.db"], "no backup either"


def test_the_ops_cli_migrates_through_the_environment(tmp_path, monkeypatch, capsys) -> None:
    path = _older_database(tmp_path / "control-plane.db")
    monkeypatch.setenv(DATABASE_PATH_VARIABLE, str(path))
    monkeypatch.setattr(sys, "argv", ["hoh-ops", "migrate"])

    operations.main()

    assert '"migrated": true' in capsys.readouterr().out
    assert _version(path) == SCHEMA_VERSION


def test_no_database_path_is_refused_rather_than_defaulted(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit, match=DATABASE_PATH_VARIABLE):
        database_path_from_environment()
    with pytest.raises(SystemExit, match=DATABASE_PATH_VARIABLE):
        create_app(local_mode=True)
    monkeypatch.setattr(sys, "argv", ["hoh-scheduler", "--once"])
    with pytest.raises(SystemExit, match=DATABASE_PATH_VARIABLE):
        scheduler.main()

    assert not (tmp_path / "data").exists(), "no stray database under the working directory"


def test_the_desktop_upgrade_migrates_explicitly_into_its_backup_folder() -> None:
    upgrader = (
        Path(__file__).resolve().parents[1] / "deploy" / "desktop" / "upgrade_local.py"
    ).read_text(encoding="utf-8")
    migrate = upgrader.index("'hoh_control_plane.operations', 'migrate'")
    assert "'--backup-dir', str(backup)" in upgrader
    assert upgrader.index("source.backup(target)") < migrate
    assert migrate < upgrader.index("'--no-browser'"), "migrated before the new host starts"
