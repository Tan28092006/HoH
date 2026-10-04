import hashlib
from pathlib import Path

import pytest

from hoh_control_plane.qa_runner import TestExecution as Execution
from hoh_control_plane.supervisor import ADMIN
from test_integration_async import REGRESSION_COMMAND, _candidate_ready_for_integration, _claimed_for_integration, _git


def ready(service,actors,seeded,tmp_path):
    repo,ticket,candidate,verified = _candidate_ready_for_integration(service,actors,seeded,tmp_path)
    claim = _claimed_for_integration(service,actors,ticket,verified)
    job = service.queue_merge_candidate(actors['integrator'],ticket['ticket_id'],claim['lease']['lease_id'],claim['ticket']['version'])['merge_job']
    return repo,ticket,candidate['candidate'],claim,job


def test_queue_binds_current_candidate_not_older_row_with_same_sha(service,actors,seeded,tmp_path):
    repo,ticket,submitted,verified = _candidate_ready_for_integration(service,actors,seeded,tmp_path)
    old = submitted['candidate']['candidate_id']
    with service.store.connection(immediate=True) as c:
        c.execute("""INSERT INTO candidates
          (candidate_id,ticket_id,base_commit,commit_sha,branch_ref,developer_claims,submitted_by,submitted_at,frozen_at)
          SELECT 'cand_current',ticket_id,base_commit,commit_sha,branch_ref,developer_claims,submitted_by,
          '2099-01-01T00:00:00Z','2099-01-01T00:00:00Z' FROM candidates WHERE candidate_id=?""",(old,))
        c.execute("""INSERT INTO candidate_test_profiles SELECT 'cand_current',test_command,
          timeout_seconds,configured_at FROM candidate_test_profiles WHERE candidate_id=?""",(old,))
        for table in ('qa_findings','qa_test_runs','evidence'):
            c.execute(f"UPDATE {table} SET candidate_id='cand_current' WHERE candidate_id=?",(old,))
        c.execute("UPDATE audit_events SET payload=replace(payload,?,'cand_current') WHERE aggregate_id=?",(old,ticket['ticket_id']))
    claim = _claimed_for_integration(service,actors,ticket,verified)
    job = service.queue_merge_candidate(actors['integrator'],ticket['ticket_id'],claim['lease']['lease_id'],claim['ticket']['version'])['merge_job']
    assert job['candidate_id'] == 'cand_current'


@pytest.mark.parametrize('outcome', ['passed','test_failure','resource_exhausted'])
def test_detached_merge_runs_isolated_gate_before_mutating_main(service,actors,seeded,tmp_path,monkeypatch,outcome):
    repo,ticket,candidate,claim,job = ready(service,actors,seeded,tmp_path)
    calls = []
    def execute(command,path,timeout):
        calls.append(path)
        assert Path(path).resolve() != repo.resolve()
        assert _git(Path(path),'rev-parse','HEAD') == candidate['commit_sha']
        assert _git(repo,'rev-parse','main') == candidate['base_commit']
        assert command == REGRESSION_COMMAND
        return Execution(command_digest=hashlib.sha256('\0'.join(command).encode()).hexdigest(),
            exit_code=0 if outcome=='passed' else 1,timed_out=False,duration_ms=1,
            stdout=outcome,stderr='',termination_reason=outcome)
    monkeypatch.setattr(service.project_tests,'execute',execute)
    result = service.execute_queued_merge(job['merge_job_id'])
    assert len(calls) == 1
    with service.store.connection() as c:
        passed = c.execute("SELECT COUNT(*) FROM audit_events WHERE event_type='integration.gate_passed' AND aggregate_id=?",(job['merge_job_id'],)).fetchone()[0]
        status = c.execute('SELECT status FROM tickets WHERE ticket_id=?',(ticket['ticket_id'],)).fetchone()[0]
        assert c.execute('SELECT COUNT(*) FROM qa_test_runs WHERE candidate_id=?',(candidate['candidate_id'],)).fetchone()[0] == 2
    assert passed == (1 if outcome=='passed' else 0)
    if outcome=='passed':
        assert result['merge_status']=='merged'
    else:
        assert _git(repo,'rev-parse','main')==candidate['base_commit']
        # A non-regression gate failure is infrastructure: BLOCKED for Planner.
        assert status==('REWORK' if outcome=='test_failure' else 'BLOCKED')


def test_recovery_will_not_certify_external_movement_without_job_gate(service,actors,seeded,tmp_path,monkeypatch):
    repo,ticket,candidate,claim,job=ready(service,actors,seeded,tmp_path)
    monkeypatch.setattr(service.project_tests,'execute',lambda *a: (_ for _ in ()).throw(SystemExit('worker died before gate')))
    with pytest.raises(SystemExit):
        service.execute_queued_merge(job['merge_job_id'])
    _git(repo,'merge','--ff-only',candidate['commit_sha'])
    recovered=service.reconcile_merge_job(ADMIN,job['merge_job_id'])
    assert recovered['status']=='RUNNING'
    assert 'gate' in recovered['error_message'].lower()


@pytest.mark.parametrize('revocation', ['hold','lease'])
def test_gate_rechecks_authorization_before_git(service,actors,seeded,tmp_path,monkeypatch,revocation):
    repo,ticket,candidate,claim,job=ready(service,actors,seeded,tmp_path)
    def execute(command,path,timeout):
        if revocation=='hold':
            # Arrange the durable release-policy record, as in the policy tests.
            from test_integration_release_policy import _add_context
            _add_context(service,ticket,'integration_hold',candidate['commit_sha'],'2099-01-01T00:00:00Z')
        else:
            with service.store.connection(immediate=True) as c:
                c.execute('DELETE FROM leases WHERE lease_id=?',(claim['lease']['lease_id'],))
        return Execution(command_digest=hashlib.sha256('\0'.join(command).encode()).hexdigest(),
            exit_code=0,timed_out=False,duration_ms=1,stdout='passed',stderr='',termination_reason='passed')
    monkeypatch.setattr(service.project_tests,'execute',execute)
    result=service.execute_queued_merge(job['merge_job_id'])
    assert result['status']=='FAILED'
    assert _git(repo,'rev-parse','main')==candidate['base_commit']
