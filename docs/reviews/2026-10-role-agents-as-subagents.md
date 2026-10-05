# Role agents as subagents

Snapshot 2026-10-05 at 0.3.30 plus wave 5 (branch `claude/wave5`). A short review of how HoH's
Developer, QA and Integrator role agents compare with well-designed subagents,
and what wave 5 changed. Sources: Claude Code subagent sidechains, Codex
`spawn_agent`, the coordinator and swarm patterns, generator/evaluator
separation, the multi-agent failure modes in *AI Agents in Depth* chapter 10,
and loop budgets.

## Where HoH sits among the patterns

HoH is a **swarm with a coordinator**. Developer, QA and Integrator pull work
from one shared queue (offers, `work.discover`, claims with leases) rather than
being called by a parent; Planner is the coordinator that writes the plan
(tickets, contracts, dependencies), reconciles repository truth and handles
every escalation as a Planner job. Unlike a Claude Code sidechain, no role is
spawned by another role: the shared state store and the MCP service are the
only channel between them, so handoffs survive process death.

## What already matches

- **Role-filtered tools.** Each bearer gets its own FastMCP instance; tools
  outside the role are absent, not merely refused.
- **Worktree isolation.** A Developer works in a ticket-and-agent-bound Git
  worktree; candidates are frozen commits, never shared mutable trees.
- **The ticket as the handoff package.** The bounded, role-projected packet
  (contract, frozen candidate, findings, typed references) is the brief; chat
  history is never evidence.
- **Execution-feedback QA.** QA runs the frozen project command and the
  contract scenarios on the exact SHA and attaches typed evidence; a verdict is
  refused without it.
- **Async job IDs.** Worktree provisioning, candidate submission, test runs and
  merges return a durable job ID at once and are polled, so a long step never
  holds a chat or a write lock.
- **Evidence retention.** Candidates, findings, evidence bundles and audit
  events outlive the agents; retirement preserves worktrees and candidates.

## Gaps and what wave 5 did

1. **Fresh context per ticket.** A role was one long chat looping
   work → wait → work, so context grew with every ticket (cost roughly
   quadratic in tickets handled) and one ticket's conclusions bled into the
   next. Now every claim returns a `worker_brief` (ticket, role, lease, packet
   snapshot, worktree, skills, exact next calls, result fields). The skills and
   generated prompts make the waiting chat a thin dispatcher that hands the
   brief to a fresh subagent (Claude Code Agent/Task, Codex `spawn_agent`) and
   keeps only the structured result, or starts a new session per ticket.
2. **The checker anchored by the maker.** QA read `developer_claims` inline.
   QA now gets them only as `ctx:candidate:<id>:developer_claims`, and the QA
   card says to record its own observations before resolving them.
3. **No rework budget.** Dev and QA could loop forever. `MAX_QA_REJECTIONS = 3`
   is enforced by the service: the third failed QA verdict sends the ticket to
   `BLOCKED` (`rework_budget_exhausted`) with a high-severity Planner job instead
   of `REWORK`. Planner resuming it records `ticket.rework_budget_reset` and a
   Planner decision; nothing resets silently.
4. **One model for every role.** `local_agent setup --model` writes a Codex
   profile `model`, or a Claude Code worker subagent with a `model` field.
   LOCAL_SETUP.md recommends the strongest model for Planner and a different
   model for QA than for Developer.
5. **No provenance in packets.** Packets now carry `sources` (which sections
   each author wrote) and a `source` on every context entry and lesson, so
   Developer-written text reaches QA and Planner labelled as advisory.
6. **Full digests everywhere** (owner request, added to the wave). Packets and
   results repeated 40-hex commits and 64-hex digests, about ten to sixteen
   tokens each. `agent_facing()` now shows them
   as 12-character prefixes in packets (before budget fitting) and in every MCP
   result; the store, audit events and evidence reports keep full values.
   Tools that take a commit accept a 7+ character prefix and resolve it with
   `git rev-parse` before any comparison, refusing unknown or ambiguous ones. A
   typical QA_READY packet drops from 645 to 617 estimated tokens.

## What remains

- **Token cost is not measured per ticket.** `estimated_tokens` sizes each
  packet, but nothing sums what a ticket cost across its packets, polls and
  worker transcripts, so the saving from fresh workers is argued, not measured.
- **pass^k reliability is not measured.** HoH records one verdict per
  candidate; it never reruns a role k times on the same ticket to measure how
  often all k attempts succeed, which is the reliability figure a swarm needs.
- Only `planner.resume_ticket_after_failure` resets the rework budget. A
  ticket unblocked another way keeps its count, so its next rejection blocks it
  again; that is conservative, but it is not yet stated in the Planner card.
- The dispatcher rule is still prose in the skills: the server cannot tell
  whether a chat delegated, so a client that ignores it degrades to the old
  long-lived loop.
