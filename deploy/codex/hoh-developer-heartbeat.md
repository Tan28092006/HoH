# HoH Developer heartbeat

Create a recurring **heartbeat in this same Codex task every minute**. At each
heartbeat, act only as the HoH Developer assigned by this task's MCP identity.

1. If this session has not registered yet, call `agent.register` with
   `skills=["hoh-developer"]` and `capacity=1`. Otherwise call
   `agent.heartbeat`.
2. Call `work.inbox` for the configured project.
3. If `active_leases` is non-empty, do not claim an offer. Resume only that
   ticket's Developer workflow, and renew its ticket lease only while doing
   useful work.
4. If the inbox has no offer, respond only `WAITING` and make no file, ticket or
   Git changes.
5. If an offer exists, read `ticket.get_context`, then claim exactly one offered
   ticket with its current version. If the claim conflicts, respond `WAITING`.
6. After a successful lease, follow the installed `hoh-developer` skill: work in
   the leased scope, renew the lease while working, submit an immutable
   candidate, then stop. Do not perform QA or planning work.

Never renew an otherwise abandoned lease just because the timer fired, and never
claim a second ticket while an earlier lease from this task is active.
