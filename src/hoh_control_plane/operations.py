"""Local operator controls. Access requires the host's database credentials."""
import argparse
from contextlib import closing
from datetime import datetime, UTC
import json
from pathlib import Path
import sqlite3
import time

from hoh_control_plane.supervisor import ADMIN, operations_status, set_paused


def check_health(service, queue_age_seconds=900):
    report = operations_status(service)
    alerts = []
    if report['supervisor_stale']:
        alerts.append('supervisor_stale')
    if report['needs_attention']:
        alerts.append('merge_reconciliation_required')
    for kind, rows in report['queues'].items():
        for row in rows:
            if row['status'] == 'QUEUED' and row['oldest']:
                age = (datetime.now(UTC) - datetime.fromisoformat(row['oldest'].replace('Z', '+00:00'))).total_seconds()
                if age > queue_age_seconds:
                    alerts.append(kind + '_queue_overdue')
    return {**report, 'healthy': not alerts, 'alerts': alerts}


def _sqlite_schema_version(database):
    """The schema version recorded in a SQLite file, read without changing it."""
    from hoh_control_plane.store_migrations import read_schema_version
    with closing(sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)) as connection:
        def columns_of(table):
            return {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}
        return read_schema_version(connection, columns_of)


def migrate_database(database_path=None, backup_dir=None, external_backup=False):
    """Upgrade a database to this build's schema, after backing it up.

    Serving processes refuse a database older than the code (see
    ``SQLiteStore.initialize``); this is the one explicit upgrade path. A SQLite
    file is first copied with ``sqlite3.backup()`` and the copy integrity-checked;
    nothing is migrated if that fails. Stop every HoH process using the database
    before running it.
    """
    from hoh_control_plane.store import SQLiteStore
    from hoh_control_plane.store_factory import database_path_from_environment, store_from_environment
    from hoh_control_plane.store_migrations import SCHEMA_VERSION, require_supported_schema_version
    database = database_path_from_environment(database_path)
    if database is None:
        if not external_backup:
            raise SystemExit('PostgreSQL is configured: take a backup with pg_dump first, then rerun '
                             'with --external-backup. Nothing was changed.')
        store_from_environment(allow_migration=True)
        return {'migrated': True, 'schema_version': SCHEMA_VERSION, 'backup': 'external'}
    if not database.is_file():
        raise SystemExit(f'No database at {database}; nothing to migrate. Nothing was changed.')
    found = _sqlite_schema_version(database)
    require_supported_schema_version(found, str(database))
    if found == SCHEMA_VERSION:
        return {'migrated': False, 'database': str(database), 'schema_version': found, 'backup': None}
    target_dir = Path(backup_dir) if backup_dir else database.parent
    target_dir.mkdir(parents=True, exist_ok=True)
    backup = target_dir / (f'{database.stem}-schema-v{found}-before-v{SCHEMA_VERSION}-'
                           + time.strftime('%Y%m%d-%H%M%S') + '.db')
    if backup.exists():
        raise SystemExit(f'Backup {backup} already exists; not overwriting it. Nothing was changed.')
    with closing(sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)) as source:
        with closing(sqlite3.connect(backup)) as copy:
            source.backup(copy)
            if copy.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise SystemExit(f'Backup {backup} failed its integrity check. Nothing was migrated.')
    SQLiteStore(database).initialize(allow_migration=True)
    return {'migrated': True, 'database': str(database), 'from_schema_version': found,
            'schema_version': SCHEMA_VERSION, 'backup': str(backup)}


def main():
    from hoh_control_plane.service import ControlPlaneService
    from hoh_control_plane.store_factory import store_from_environment
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    migrate = commands.add_parser('migrate', help='back up, then upgrade the database schema')
    migrate.add_argument('--database', help='SQLite file; defaults to HOH_DATABASE_PATH')
    migrate.add_argument('--backup-dir', help='where the pre-migration copy goes; defaults beside the database')
    migrate.add_argument('--external-backup', action='store_true',
                         help='PostgreSQL only: you already took a backup')
    commands.add_parser('status')
    health = commands.add_parser('check')
    health.add_argument('--queue-age-seconds', type=int, default=900)
    for action in ('pause', 'resume'):
        commands.add_parser(action).add_argument('--reason', required=True)
    commands.add_parser('reconcile').add_argument('--merge-job-id', required=True)
    args = parser.parse_args()
    if args.command == 'migrate':
        from hoh_control_plane.domain import DomainError
        try:
            result = migrate_database(args.database, args.backup_dir, args.external_backup)
        except DomainError as error:
            # Schema errors name only a path and version numbers, never credentials.
            print(json.dumps({'migrated': False, 'error': str(error)}))
            raise SystemExit(2) from None
        print(json.dumps(result, indent=2))
        return
    try:
        service = ControlPlaneService(store_from_environment())
        if args.command in {'pause', 'resume'}:
            set_paused(service, args.command == 'pause', reason=args.reason)
            result = operations_status(service)
        elif args.command == 'reconcile':
            result = service.reconcile_merge_job(ADMIN, args.merge_job_id)
        elif args.command == 'check':
            if args.queue_age_seconds < 1:
                parser.error('--queue-age-seconds must be positive')
            result = check_health(service, args.queue_age_seconds)
        else:
            result = operations_status(service)
        print(json.dumps(result, indent=2))
        if args.command == 'check' and not result['healthy']:
            raise SystemExit(1)
    except Exception as error:
        print(json.dumps({'healthy': False, 'alerts': ['operations_unavailable'], 'error_type': type(error).__name__}))
        raise SystemExit(2) from None


if __name__ == '__main__':
    main()
