# HoH 0.3.20 — short identifiers and resolvable citations

## Identifiers

Every identifier the control plane allocates is now a prefix plus a per-prefix
counter: `cand_0007`, `lease_0042`, `ctx_0311`, `ctxsnap_0002`, `wait_0009`,
`delivery_0015`, `attempt_0003`. The counter is kept in a new `id_sequences`
table and incremented inside the same write transaction as the row it names,
so uniqueness is a property of the store rather than of randomness. It widens
past four digits instead of wrapping.

Why: identifiers are quoted in packets, decisions, evidence references and tool
calls far more often than they are generated. A 16-hex tail cost roughly ten
tokens per quotation and was mistyped often enough by a model copying it back
that "unknown id" retries were a visible share of failed calls. Four digits cost
one or two tokens, and the ordinal makes "the newer candidate" visible from the
id itself.

Existing identifiers stay valid; an id is opaque and is never parsed for
length. Packets for old tickets therefore mix both shapes until those rows are
superseded.

Context snapshots are deduplicated by a content digest stored in a new
`context_snapshots.content_digest` column and named by the counter. Re-reading
unchanged context still returns the same `context_snapshot_id`. Feature packets
now record a snapshot too, so no packet carries a placeholder id. The wait id
and delivery id used by `work.wait_for_event` follow the same scheme; the
authenticated identity, not the id's entropy, is what protects a resume.

The failure-report fingerprint in `failure:<hex>` source references is
truncated to 16 hex characters. It is a dedupe key, not an identity.

## Packets

- `candidate.changed_paths` is compacted to `changed_paths_summary`
  (`count`, `within_file_scope`) when every path is inside the ticket's write
  scope, which submission already enforced. A path outside scope keeps the full
  list, because that is the one fact worth the room. The list resolves through
  `ctx:candidate:<id>:changed_paths`.
- New tool `context.resolve` resolves one reference a packet cited in
  `omitted_sections`: `ctx:entry:<id>`, `ctx:candidate:<id>:developer_claims`,
  `ctx:candidate:<id>:changed_paths`, `ctx:finding:<id>` and, for Planner or
  Admin, `ctx:ticket:<id>:v<n>:history`. Visibility follows the packet: an
  entry type a role never sees in its packet is refused here too. Until this
  release no tool answered these citations.
- Discovery drops `pending_ticket_refs` (it repeated every `tickets[].ticket_ref`)
  in favour of `pending_ticket_count`, and Planner/Admin rows no longer carry
  `recommended_calls`, which was the same `ticket.get_context` call on every row.

Measured on the live database (estimated tokens, before → after):

| Payload | Before | After |
| --- | ---: | ---: |
| Developer packet, `area303-upstream-reconciliation-candidate` (QA_READY) | 3,826 | 3,603 |
| Planner `work.discover`, project `hoh-control-plane` | 6,726 | 5,232 |
| Planner `work.discover`, project `legal-uit` | 1,005 | 838 |

Identifier savings apply to rows created after the upgrade; a QA_READY packet
on the live database quotes 13–21 generated identifiers, so a fresh ticket of
the same shape saves a further 100–150 tokens per read.

## Skills

`hoh-control-plane` names `context.resolve` in its context-discipline
paragraph. No other card or reference changed.

## Compatibility

- Schema: additive only (`id_sequences`, `context_snapshots.content_digest`,
  one unique index). Applied by the normal `initialize()` migration.
- SQLite 3.35+ is required for `RETURNING`; the installed runtime bundles
  3.45. PostgreSQL uses the same statement under the existing advisory lock.
- Tests: `tests/test_short_identifiers.py` covers the counter, snapshot naming,
  reference resolution and discovery rows. Two existing tests changed
  assertions (`test_discovery_contract`, `test_git_worktrees`).

See `docs/CONTEXT_DISTRIBUTION_REVIEW.md` for the long-running-operation review
that motivated the packet and resolver changes.
