"""Fault cases for isolated checkouts and the Git/database commit boundary."""
from pathlib import Path

import pytest

from hoh_control_plane.domain import Actor, Role, ValidationError
from test_integration_async import _candidate_ready_for_integration, _claimed_for_integration, _git, _repository

ADMIN = Actor('recovery-test', Role.ADMIN, frozenset({'*'}))


def queued_merge(service, actors, seeded, tmp_path):
    repository, ticket, candidate, verified = _candidate_ready_for_integration(service, actors, seeded, tmp_path)
    claim = _claimed_for_integration(service, actors, ticket, verified)
    job = service.queue_merge_candidate(actors['integrator'], ticket['ticket_id'],
                                       claim['lease']['lease_id'], claim['ticket']['version'])['merge_job']
    return repository, candidate['candidate'], job


def test_qa_checkout_has_private_git_metadata(service, tmp_path):
    repo = _repository(tmp_path)
    head = _git(repo, 'rev-parse', 'HEAD')
    with service.git_worktrees.qa_checkout(repository_path=str(repo), test_run_id='testrun_private', candidate_commit=head) as path:
        snapshot = Path(path)
        assert (snapshot / '.git').is_dir(), 'QA must not share the protected repository Git directory'
        assert not (snapshot / '.git/objects/info/alternates').exists()
        _git(snapshot, 'update-ref', 'refs/heads/qa-test-branch', head)
        assert not _git(repo, 'branch', '--list', 'qa-test-branch')
    assert not snapshot.exists()


def test_existing_qa_checkout_is_preserved(service, tmp_path):
    repo = _repository(tmp_path)
    path = service.git_worktrees.qa_path('testrun_existing')
    path.mkdir(parents=True)
    (path / 'keep.txt').write_text('in-flight work')
    with pytest.raises(ValidationError):
        with service.git_worktrees.qa_checkout(repository_path=str(repo), test_run_id='testrun_existing',
                candidate_commit=_git(repo, 'rev-parse', 'HEAD')):
            pytest.fail('Existing resources cannot be taken over by a test')
    assert (path / 'keep.txt').read_text() == 'in-flight work'


def test_same_identity_new_lease_gets_new_worktree(service, actors, seeded, tmp_path):
    repo = _repository(tmp_path)
    service.configure_repository(actors['planner'], 'project-1', str(repo))
    ticket = seeded['ticket']
    first = service.claim_ticket(actors['developer'], ticket['ticket_id'], ticket['version'], 900)
    one = service.prepare_worktree(actors['developer'], ticket['ticket_id'], first['lease']['lease_id'], first['ticket']['version'])
    dirty = Path(one['worktree']['worktree_path']) / 'keep.txt'
    dirty.write_text('unfinished work')
    with service.store.connection(immediate=True) as db:
        db.execute("UPDATE leases SET expires_at = '2000-01-01T00:00:00Z' WHERE lease_id = ?", (first['lease']['lease_id'],))
    service.sweep_expired_leases(ADMIN)
    with service.store.connection() as db:
        ticket = service._ticket(db, ticket['ticket_id'])
    second = service.claim_ticket(actors['developer'], ticket['ticket_id'], ticket['version'], 900)
    two = service.prepare_worktree(actors['developer'], ticket['ticket_id'], second['lease']['lease_id'], second['ticket']['version'])
    assert two['worktree']['worktree_path'] != one['worktree']['worktree_path']
    assert dirty.read_text() == 'unfinished work'


def test_reconcile_merge_after_git_completed_and_worker_crashed(service, actors, seeded, tmp_path, monkeypatch):
    repo, candidate, job = queued_merge(service, actors, seeded, tmp_path)
    complete = service._complete_queued_merge
    monkeypatch.setattr(service, '_complete_queued_merge', lambda *a, **k: (_ for _ in ()).throw(SystemExit('worker died')))
    with pytest.raises(SystemExit):
        service.execute_queued_merge(job['merge_job_id'])
    assert _git(repo, 'rev-parse', 'HEAD') == candidate['commit_sha']
    monkeypatch.setattr(service, '_complete_queued_merge', complete)
    with service.store.connection(immediate=True) as db:
        db.execute("UPDATE leases SET expires_at = '2000-01-01T00:00:00Z' WHERE lease_id = ?", (job['lease_id'],))
    service.sweep_expired_leases(ADMIN)
    recovered = service.reconcile_merge_job(ADMIN, job['merge_job_id'])
    assert recovered['status'] == 'COMPLETED'
    assert recovered['merge_status'] == 'merged'
    assert service.reconcile_merge_job(ADMIN, job['merge_job_id'])['status'] == 'COMPLETED'
    with service.store.connection() as db:
        assert service._ticket(db, job['ticket_id'])['status'] == 'DONE'
        assert db.execute("SELECT count(*) FROM events WHERE event_type = 'integration.completed' AND aggregate_id = ?", (job['ticket_id'],)).fetchone()[0] == 1


def test_reconcile_pre_git_crash_does_not_merge_without_live_authorization(service, actors, seeded, tmp_path, monkeypatch):
    repo, candidate, job = queued_merge(service, actors, seeded, tmp_path)
    monkeypatch.setattr(service.git_worktrees, 'fast_forward_default_branch', lambda **k: (_ for _ in ()).throw(SystemExit('worker died')))
    with pytest.raises(SystemExit):
        service.execute_queued_merge(job['merge_job_id'])
    recovered = service.reconcile_merge_job(ADMIN, job['merge_job_id'])
    assert recovered['status'] == 'FAILED'
    assert _git(repo, 'rev-parse', 'HEAD') == candidate['base_commit']


def test_recovery_does_not_take_repository_lock_from_a_live_worker(service, actors, seeded, tmp_path):
    repo, _, job = queued_merge(service, actors, seeded, tmp_path)
    with service.git_worktrees.repository_lock(str(repo)):
        assert service.reconcile_merge_job(ADMIN, job['merge_job_id'])['status'] == 'QUEUED'


def test_dirty_integration_checkout_cannot_be_silently_overwritten(service, tmp_path):
    repo = _repository(tmp_path)
    base = _git(repo, 'rev-parse', 'HEAD')
    (repo / 'keep.txt').write_text('operator work')
    with pytest.raises(ValidationError, match='clean'):
        service.git_worktrees.fast_forward_default_branch(repository_path=str(repo), default_branch='main',
            expected_base_commit=base, candidate_commit=base)
