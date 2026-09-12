"""Local operator controls. Access requires the host's database credentials."""
import argparse
from datetime import datetime, UTC
import json

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


def main():
    from hoh_control_plane.service import ControlPlaneService
    from hoh_control_plane.store_factory import store_from_environment
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('status')
    health = commands.add_parser('check')
    health.add_argument('--queue-age-seconds', type=int, default=900)
    for action in ('pause', 'resume'):
        commands.add_parser(action).add_argument('--reason', required=True)
    commands.add_parser('reconcile').add_argument('--merge-job-id', required=True)
    args = parser.parse_args()
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
