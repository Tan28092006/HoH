# Windows desktop operator controls (0.3.4)

> Snapshot 2026-09-13 at 0.3.4, not kept in step with the code. For the host
> installed on this machine read [LOCAL_RUNTIME.md](../../docs/LOCAL_RUNTIME.md);
> for supervision and recovery read [RUNTIME_OPERATIONS.md](../../docs/RUNTIME_OPERATIONS.md).

These launcher files describe the existing single-user installation at
`D:\HoH-service`; they are not a universal Windows installer. The Python package
provides `desktop_control.create_desktop_app` and `local_agent` separately from
the normal API. Copy the launcher files into the installation root only after
stopping its host and installing the matching wheel. Keep `config`, database,
artifact roots and Git worktrees in place. Never run `--initialize` or
`--bootstrap-data` over an existing installation.

For a packaged upgrade, extract the archive and run its `upgrade_local.py` with
the installed Python and `--root D:\HoH-service`. It validates the manifest,
requests a draining shutdown, checks both ports closed, backs up SQLite and the
launcher, installs only the supplied wheel, and checks the restarted version.
It does not recreate the database or relocate any Git worktree. Then run the
profile migration below and reload the clients. `verify_shared_profiles.py`
checks bootstrap and role surfaces using read-only HTTP calls; its optional
`--connections 17 --hold-seconds 20` supports measuring the shared host under
multiple connected clients.

## Operator workflow

The Desktop shortcut starts a hidden host and opens the browser. Closing its
browser tab does not stop services. The UI's **Tắt HoH** confirmation pauses
worker admission, waits for recorded running executions to finish, then stops
UI, HTTP MCP, scheduler and supervisor. Queued jobs remain durable. A stalled
worker can delay shutdown until its existing timeout/recovery resolves it.
The next explicit Desktop launch restores admission only if this shutdown
paused it; a pre-existing operator pause remains in place. `--stop` and the old
Stop shortcut remain immediate maintenance controls, not the draining UI flow.

There is no required console window for the host. Agent sessions remain separate:
closing an agent's terminal ends that session. Agent leases are not completed
merely by stopping HoH; resume and validate them on reconnect.

**Thêm dự án** creates an audited project record, and the selector lists every
project even when it has no tickets. It does not create a Git repository or
change an existing worktree. Select the project, open **Prompt cho agent**, and
copy the desired Planner, Developer, QA or Integrator prompt. The Planner must
resolve the actual repository and test policy before creating executable work.

## Session installation and project isolation

Generated prompts run `python -m hoh_control_plane.local_agent setup` using the
installed runtime. The installer supports Codex and Claude Code, preserves
unrelated client settings, refuses conflicting MCP entries, and can explicitly
back up and upgrade only HoH base/role skills. A profile has a stable identity
derived from project, role and instance; its credential scope contains exactly
that project. A second concurrent agent in the same role needs its own instance.
A normal restart of the same chat keeps its existing instance: bootstrap exposes
`event_wait.wait_id`, and the next `work.wait_for_event` resumes that wait with
`resume_wait_id`. This atomically fences the old request; it does not create a
second worker or alter any lease, job or worktree. A genuinely concurrent chat
still needs a different instance.

Each prompt requires `session.bootstrap(project_id=...)` on the named server and
checks identity role, identity ID, allowed project IDs and selected project ID.
The chat directory is never used to guess a project. Installing profiles may
require a client MCP reload/restart before the new tools become available.
New profiles refuse automatic restart after operator shutdown and exit when
the host finishes stopping. Legacy stdio clients need to reconnect using these
profiles to get that lifecycle behavior; direct old MCP processes are not owned
by this host.

Codex profiles allow a 24-hour MCP tool timeout. Claude Code profiles use a
24-hour per-server timeout and preserve a longer configured value. HoH emits
transport progress during its 20-second recovery heartbeat so a supported GUI
client does not treat an otherwise healthy wait as silent. Stopping a chat,
sleeping the machine or stopping HoH still ends that chat's wait; a stopped GUI
conversation cannot be reopened by MCP.

New desktop setup prompts use `--transport shared-http`. Each chat connects
directly to the existing loopback MCP host, so no Python/PowerShell server is
spawned per chat. Setup provisions a private role/project credential in
`config/agent-credentials.json` and the client configuration. Never paste those
files into a chat or include them in a release archive. The host reloads this
map before authenticating each request and lazily builds one tool surface per
identity. HTTP sessions are stateless; idle GUI connections do not accumulate
server-side session managers. Revoked credentials cannot reuse a cached actor.

Existing `serve` profiles remain compatible. To migrate recognized profiles,
run `python -I -m hoh_control_plane.profile_migration --root D:\HoH-service
--client codex --legacy-repository D:\HoH` (or `--client claude-code`). The first
run previews identity-preserving changes; add `--apply` to write with backups.
Only known generated profiles are changed. Reload the clients after migration
to release their old stdio processes. Open HoH with its Desktop icon before
connecting: HTTP profiles do not automatically start an operator-stopped host.

Creating a project does not broaden existing HTTP token scopes. Remote agents still need separately
provisioned credentials and an authorized network path. Both listeners remain
loopback-only. Do not expose the local operator UI directly to a network.

## Validation

The desktop tests use disposable databases and client homes. They exercise
project persistence, four bound prompts, role/project enforcement over real
stdio MCP, CSRF/Host checks, shutdown drain/reopen, explicit-stop reconnect,
skill upgrade backup/rollback, same-chat wait replacement and SQLite connection
closure. Git validation runs outside the MCP event loop with detached stdin,
bounded output and a Windows process-tree timeout; partial Git output is never
accepted as repository evidence. Browser tests exercise project creation, all
role prompts and cancellation/confirmation at desktop/tablet/mobile widths.
Existing API, agent kit, bootstrap and supervisor tests cover compatibility,
including a pause arriving during queue scanning.

The local wrapper only permits its two explicit POST actions; the underlying
REST mutation routes remain unavailable through the desktop listener. Writes
require a matching loopback Host, same-origin Origin and per-process session
nonce. No bearer token is sent to the browser. This is a trusted single-host
pilot, without HA or a worker sandbox.

## Candidate environment boundary

The QA runner discards ambient `PYTHONPATH`, `PYTHONHOME`, `VIRTUAL_ENV`, HoH
credentials and coverage injection before starting a candidate command. It adds
only the candidate worktree's own `src` directory and disables user-site imports.
It deliberately does not add `-I` to the candidate command: framework-specific
path configuration and frozen command profiles remain authoritative. Windows
native exit codes survive the managed gate as unsigned 32-bit statuses.

This fixes cross-version host dependency leakage, but does not finish the
`area303-hoh-runner-readiness` contract. Project-configurable provenance after
pytest configuration, an actual-child environment manifest, resource admission
and infrastructure-only QA retry routing still need their own changes. Do not
mark an existing candidate accepted using historical insufficient evidence.
