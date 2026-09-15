# Board and context lookup

`work.discover` answers where to look, not what a ticket means. Scope it with
`blocked_only`, lifecycle status, awaiting role, feature, priority or cursor;
do not raise limits to load the whole board. Use `feature.get_context` for the
one feature being planned, then resolve the ticket that needs a decision.

Normal packets contain newest role-relevant decisions. Use `include_history=true`
only for a concrete audit question, never as routine startup. Fetch primary
evidence only when summaries disagree or an exception needs justification.
Record decisions with refs, affected ticket/SHA, reason, next owner and any
superseded decision. Track context size and repeated drift as operational signals,
not as a reason to drop required validation.
