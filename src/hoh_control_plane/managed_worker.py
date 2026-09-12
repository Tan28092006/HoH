"""Execute one durable job while holding its host ownership lock."""
import argparse

from hoh_control_plane.service import ControlPlaneService
from hoh_control_plane.store_factory import store_from_environment
from hoh_control_plane.supervisor import JOBS, job_lock


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', required=True)
    parser.add_argument('--kind', choices=JOBS, required=True)
    parser.add_argument('--job-id', required=True)
    args = parser.parse_args()
    service = ControlPlaneService(store_from_environment(args.database))
    with job_lock(service, args.kind, args.job_id):
        getattr(service, JOBS[args.kind][2])(args.job_id)


if __name__ == '__main__':
    main()
