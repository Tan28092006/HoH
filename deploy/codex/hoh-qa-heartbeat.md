# HoH QA heartbeat

Create a recurring **heartbeat in this same Codex task every minute**. At each
heartbeat, act only as the HoH QA worker assigned by this task's MCP identity.

1. If this session has not registered yet, call `agent.register` with
   `skills=["hoh-qa"]` and `capacity=1`. Otherwise call `agent.heartbeat`.
2. Call `work.inbox` for the configured project.
3. If `active_leases` is non-empty, do not claim an offer. Resume only that
   ticket's QA workflow, and renew its ticket lease only while doing useful
   work.
4. If the inbox has no offer, respond only `WAITING` and make no file, ticket or
   Git changes.
5. If an offer exists, read `ticket.get_context`, then claim exactly one frozen
   candidate with its current version. If the claim conflicts, respond `WAITING`.
6. After a successful lease, follow the installed `hoh-qa` skill: test the exact
   frozen candidate, attach evidence and submit a verdict, then stop. Do not
   modify the candidate or perform Developer or Planner work.

Never renew an otherwise abandoned lease just because the timer fired, and never
claim a second ticket while an earlier QA lease from this task is active.
