# Native worker supervision and recovery

## Runner contract in 0.3.5

The supervised host admits at most one worker that runs a project suite at a
time: a QA run, or a merge whose integration gate executes the suite. A merge
whose gate reuses a passing QA run of the same commit and command only moves
Git, so it is admitted in a separate merge lane and does not wait behind a QA
suite; merges still run one at a time, and that worker is started with
`--without-test-slot` so it leaves the job queued rather than run a suite. Git
preparation may use the remaining worker capacity. This bounds test-suite
overlap, not memory usage of unrelated applications. Before pytest launch the
runner checks a small free-memory/commit admission floor and retains measured
host headroom, child peak memory and queryable process/job limits. Allocation
failure is infrastructure evidence and must not be mistaken for an assertion.

Python pytest commands preserve their registered interpreter flags and pytest
arguments. A shipped observer records the actual child interpreter, dependency
versions, imports after pytest setup, candidate SHA before/after, tracked-file
cleanliness, skipped-test reasons and capture status. Declare unusual package
layouts in the frozen project's `[tool.hoh.qa].provenance_modules`; otherwise
wheel package declarations and src packages provide discovery. This observer
is provenance checking for trusted project tests, not a hostile-code sandbox.

Infrastructure failures park QA as BLOCKED, preserve its candidate and evidence,
release the QA lease and notify Planner. A terminal run stays readable by its
original requester. Planner records a current readiness decision with a source
reference, then calls `planner.recover_candidate_for_qa` with the failed evidence
and `readiness_reference`. The same candidate returns to QA; Developer is not
asked to repair the host or submit unchanged source. Integration likewise refuses
incomplete evidence and allocation failure without moving main or creating rework.

After an upgrade every role bootstraps again and reads a fresh ticket packet.
Bootstrap identifies runtime version; the packet exposes the latest planning
decision. Planner context revisions invalidate old write snapshots even when the
ticket's integer version did not change. Source main and the deployed release
must agree before provisioning further worktrees.

This release adds a bounded execution host, private QA Git snapshots, durable
merge intents and operator commands. It is the first operational recovery stage;
it does not claim full industrial availability, hostile-code isolation or HA.

## Windows without a VM

HoH, Git, the scheduler and supervisor run directly on Windows with Python 3.12+.
Windows Job Objects own worker process trees. Docker/WSL is not required. Use
SQLite for a single-host pilot, native PostgreSQL on Windows, or a PostgreSQL
server elsewhere. The audit used Docker only for a disposable PostgreSQL test DB.

An HTTP MCP client needs skills, credentials and connectivity. Implementation
also needs access to its assigned Git/filesystem/tool environment. Job Objects
manage lifetime; they do not stop a process running as your account from reading
your home directory. Use a separate OS identity with ACLs, a container or VM for
untrusted code. Externally launched chats are not owned by this supervisor.

## Service-host setup

Install the release on the service host, back up its database, and stop old workers
after they finish. Preserve existing worktrees and unfinished work. Added database
tables are additive. Keep every service process on the same database/artifact root.

PowerShell example for a local SQLite pilot:

```powershell
$env:HOH_DATABASE_PATH = 'D:\HoH-service\data\control-plane.db'
$env:HOH_WORKER_MODE = 'supervised'
hoh-supervisor --max-workers 2 --worker-timeout 2100
```

Run `hoh-mcp` and `hoh-scheduler` in separate service processes with the same
configuration. Follow AGENT_KIT.md for HTTP ingress and bearer identities. For
PostgreSQL, configure `HOH_DATABASE_URL` securely and an absolute
`HOH_ARTIFACT_ROOT` accessible to these processes on the execution host.

`HOH_WORKER_MODE` defaults to `supervised`: MCP queues durable
jobs and the supervisor executes them, so a supervisor must run beside every MCP
service process. `HOH_WORKER_MODE=legacy` is an explicit opt-in that launches
detached workers without the job lock, timeout or crash recovery. Unknown mode
values fail instead of selecting either mode silently.

Use an OS service manager to restart the supervisor and scheduler after failure
or reboot. This package supplies foreground commands and a health probe; it does
not automatically install Windows services. Only one supervisor may own an
artifact root. The database is also bound to its execution host, so a second host
is refused instead of consuming inaccessible local paths. This is deliberately
a single-execution-host boundary, not multi-host failover.

## Operator commands

These commands require local database access; they are not public HTTP endpoints.

```powershell
hoh-ops status
hoh-ops check --queue-age-seconds 900
hoh-ops pause --reason 'Maintenance window'
hoh-ops resume --reason 'Maintenance verified'
hoh-ops reconcile --merge-job-id merge_job_FROM_STATUS
```

Status includes queue counts/oldest creation times, supervisor heartbeat/actual
PID, pause state and merges needing attention. Check exits 0 when healthy, 1 for
health alerts and 2 when status cannot be obtained. Feed it into an independent
monitor: a dead supervisor cannot monitor itself. No email/webhook/desktop
notification is automatically sent.

Pause stops admission of new worker jobs and lets active work drain. It does not
revoke leases or stop external chats. Supervisor shutdown cancels its active
process trees; execution records remain durable. Worker count is configurable
from 1 to 32. Wall time and retained stdout/stderr are bounded. Individual CPU,
RAM, disk and network quotas remain future work.

## Worktree and QA ownership

- Reuse requires the same identity **and the same active lease**. A new lease
  gets a new assignment even for the same identity. Old dirty work is retained.
- QA and integration regression use detached snapshots with private Git objects,
  refs and configuration. No linked `.git`, alternate object store or hardlinks
  connect test code to protected Git metadata.
- Existing QA paths are refused. Integration snapshots use unique run paths and
  never remove a previous run's directory on entry.
- Normal completion removes only its own snapshot after path checks. Crash
  leftovers remain for operator recovery. Safe orphan inventory/GC is not yet
  automated; never delete a whole worktree root or prune other agents' worktrees.
- A managed worker losing lease/job authority or DB access is cancelled. Native
  process ownership cannot revoke filesystem rights from an arbitrary external
  chat running as the same OS account.

## Merge crash recovery

The queued merge worker holds a repository filesystem lock across the Git/DB
boundary. Before changing Git, it persists the original repository, branch, base
and candidate SHA together with the transition to RUNNING.

Recovery takes the same lock and leaves a live holder alone. After a crash:

1. Candidate is on the intended mainline and ticket identity/version still match:
   settle the existing job once and record DONE, even if its old lease expired.
2. Mainline is still at the authorized base: fail the interrupted job. A new merge
   requires a new authorized request; recovery itself does not merge.
3. Missing intent, changed ticket/configuration, dirty repository or unrelated
   mainline: retain RUNNING with a reconciliation message for operator review.
   No reset, forced merge or invented success is performed.

Lease sweeping holds an unsettled RUNNING merge for reconciliation before another
worker can take its ticket. Recovery runs at supervisor startup and periodically.
All integration goes through the queued path (`queue_merge_candidate`, then
`execute_queued_merge` under the repository lock with a durable merge intent).
The legacy synchronous `merge_candidate` was removed.

## Remaining production work

- Independent service watchdog, alert delivery and incident ownership.
- Per-worker OS privileges/resource quotas; external Developer session fencing.
- Consistent DB/Git/evidence backup and restore rehearsal with measured RPO/RTO.
- Multi-host job routing, artifact storage and explicit host failover.
- DB/network outage, full-disk, host reboot and sustained-load tests. Passing
  lifecycle tests is not a measured production SLO.

Primary references: [Windows Job Objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects),
[Python subprocess](https://docs.python.org/3/library/subprocess.html),
[Monitoring and alerts](https://sre.google/sre-book/monitoring-distributed-systems/).
