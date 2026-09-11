from __future__ import annotations

from hoh_control_plane.service._common import (
    Actor,
    Any,
    CLOSED_TICKET_STATUSES,
    TicketStatus,
    _CHECKPOINT_CHARS,
    _FALLBACK_SECTION_PRIORITY,
    _PROTECTED_SECTIONS,
    _ROLE_REQUIRED_SECTIONS,
    _TICKET_SECTION_PRIORITY,
    _VERBOSE_ENTRY_TYPES,
    _checkpoint,
    _estimate_tokens,
    _iso,
    _row,
    _snapshot_id,
    _validated_budget,
    require_project,
)


class ContextMixin:
    """Role-bounded context packet compilation and compaction."""

    def get_feature_context(
        self, actor: Actor, feature_ref: str, token_budget: int | None = None
    ) -> dict[str, Any]:
        budget = _validated_budget(token_budget)
        feature_id = self._strip_ref(feature_ref, "feature")
        with self.store.connection() as connection:
            feature = self._one(
                connection, "SELECT * FROM features WHERE feature_id = ?", (feature_id,)
            )
            require_project(actor, feature["project_id"])
            tickets = [
                self._ticket(connection, row["ticket_id"])
                for row in connection.execute(
                    "SELECT ticket_id FROM tickets WHERE feature_id = ? ORDER BY priority DESC",
                    (feature_id,),
                ).fetchall()
            ]
            verified = [
                constraint
                for ticket in tickets
                if ticket["status"] in {TicketStatus.VERIFIED.value, TicketStatus.DONE.value}
                for constraint in ticket["acceptance_criteria"]
            ]
        payload = {
            "context_ref": f"ctx:feature:{feature_id}:v{feature['version']}",
            "context_version": feature["version"],
            "role": actor.role.value,
            "feature": feature,
            "tickets": tickets,
            "verified_behaviors": verified,
            "open_ticket_count": sum(
                ticket["status"] != TicketStatus.DONE.value
                and TicketStatus(ticket["status"]) not in CLOSED_TICKET_STATUSES
                for ticket in tickets
            ),
        }
        return self._compile_context(
            payload,
            token_budget=budget,
            priority=("tickets", "verified_behaviors"),
            reference_prefix=f"ctx:feature:{feature_id}",
            compactors=(self._compact_feature_rollup,),
            protected=frozenset({"feature"}),
        )

    def get_ticket_context(
        self, actor: Actor, ticket_ref: str, token_budget: int | None = None
    ) -> dict[str, Any]:
        budget = _validated_budget(token_budget)
        ticket_id = self._strip_ref(ticket_ref, "ticket")
        with self.store.connection() as connection:
            project_id = self._project_for_ticket(connection, ticket_id)
            require_project(actor, project_id)
            ticket = self._ticket(connection, ticket_id)
            feature = self._one(
                connection,
                "SELECT * FROM features WHERE feature_id = ?",
                (ticket["feature_id"],),
            )
            contexts = [
                _row(row)
                for row in connection.execute(
                    """
                    SELECT * FROM context_entries
                    WHERE ticket_id = ? AND superseded_by IS NULL
                    ORDER BY created_at ASC
                    """,
                    (ticket_id,),
                ).fetchall()
            ]
            candidate = _row(
                connection.execute(
                    """
                    SELECT * FROM candidates WHERE ticket_id = ?
                    ORDER BY submitted_at DESC LIMIT 1
                    """,
                    (ticket_id,),
                ).fetchone()
            )
            if candidate is not None:
                candidate = self._candidate_with_changed_paths(connection, candidate)
            worktree = _row(
                connection.execute(
                    "SELECT * FROM worktrees WHERE ticket_id = ?", (ticket_id,)
                ).fetchone()
            )
            failures: list[dict[str, Any]] = []
            test_runs: list[dict[str, Any]] = []
            if candidate:
                test_runs = [
                    _row(row)
                    for row in connection.execute(
                        """
                        SELECT * FROM qa_test_runs
                        WHERE candidate_id = ?
                        ORDER BY created_at ASC
                        """,
                        (candidate["candidate_id"],),
                    ).fetchall()
                ]
                finding_rows = connection.execute(
                    """
                    SELECT * FROM qa_findings
                    WHERE candidate_id = ? AND status != 'verified'
                    ORDER BY created_at ASC
                    """,
                    (candidate["candidate_id"],),
                ).fetchall()
                for finding_row in finding_rows:
                    finding = _row(finding_row)
                    assert finding is not None
                    evidence_refs = finding["evidence_refs"]
                    placeholders = ",".join("?" for _ in evidence_refs)
                    evidence = []
                    if evidence_refs:
                        evidence = [
                            _row(row)
                            for row in connection.execute(
                                f"SELECT * FROM evidence WHERE evidence_id IN ({placeholders})",
                                evidence_refs,
                            ).fetchall()
                        ]
                    finding["evidence"] = evidence
                    failures.append(finding)
            dependencies = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT dependency.ticket_id, dependency.title, dependency.status
                    FROM ticket_dependencies d
                    JOIN tickets dependency ON dependency.ticket_id = d.depends_on_ticket_id
                    WHERE d.ticket_id = ?
                    """,
                    (ticket_id,),
                ).fetchall()
            ]
        payload = {
            "context_ref": f"ctx:ticket:{ticket_id}:v{ticket['version']}",
            "context_version": ticket["version"],
            "role": actor.role.value,
            "project_id": project_id,
            "feature": feature,
            "ticket": ticket,
            "dependencies": dependencies,
            "context_entries": contexts,
            "candidate": candidate,
            "worktree": worktree,
            "test_runs": test_runs,
            "known_failures": failures,
        }
        return self._compile_context(
            payload,
            token_budget=budget,
            priority=_TICKET_SECTION_PRIORITY.get(actor.role, _FALLBACK_SECTION_PRIORITY),
            reference_prefix=f"ctx:ticket:{ticket_id}",
            compactors=(self._compact_history, self._compact_artifacts),
            protected=_PROTECTED_SECTIONS
            | _ROLE_REQUIRED_SECTIONS.get(actor.role, frozenset()),
        )

    def _compile_context(
        self,
        payload: dict[str, Any],
        *,
        token_budget: int,
        priority: tuple[str, ...],
        reference_prefix: str,
        compactors: tuple[Any, ...],
        protected: frozenset[str],
    ) -> dict[str, Any]:
        """Fit a packet to its budget, shortening narration before dropping sections.

        Protected sections are never dropped, so a packet can still exceed a budget
        smaller than that floor: a Developer without acceptance criteria, or QA
        without the frozen candidate SHA, is more dangerous than an oversized packet.
        """
        omitted: list[dict[str, Any]] = []
        payload["token_budget"] = token_budget
        payload["omitted_sections"] = omitted
        payload["generated_at"] = _iso()
        # Same-width placeholders so the envelope is measured, not added after the
        # packet has already been fitted to its budget.
        payload["estimated_tokens"] = token_budget
        payload["context_snapshot_id"] = f"ctxsnap_{'0' * 32}"

        for compact in compactors:
            if _estimate_tokens(payload) <= token_budget:
                break
            compact(payload, omitted, token_budget)

        for section in reversed(priority):
            if _estimate_tokens(payload) <= token_budget:
                break
            if section == "context_entries":
                self._drop_verbose_entries(payload, omitted, token_budget)
                continue
            if section in protected:
                continue
            value = payload.get(section)
            if not value:
                continue
            omitted.append(
                {
                    "section": section,
                    "reason": "token_budget",
                    "omitted_count": len(value) if isinstance(value, list) else 1,
                    "references": [f"{reference_prefix}:{section}"],
                }
            )
            payload[section] = [] if isinstance(value, list) else None

        omitted.sort(key=lambda entry: (entry["section"], entry["reason"]))
        payload["estimated_tokens"] = _estimate_tokens(payload)
        payload["context_snapshot_id"] = _snapshot_id(payload)
        return payload

    def _compact_history(
        self, payload: dict[str, Any], omitted: list[dict[str, Any]], token_budget: int
    ) -> None:
        """Shorten narration to a checkpoint, keeping every entry's provenance."""
        references = [
            f"ctx:entry:{entry['entry_id']}"
            for entry in payload.get("context_entries") or []
            if entry["entry_type"] in _VERBOSE_ENTRY_TYPES
            and len(entry["content"]) > _CHECKPOINT_CHARS
        ]
        if not references:
            return
        for entry in payload["context_entries"]:
            if entry["entry_type"] in _VERBOSE_ENTRY_TYPES:
                entry["content"] = _checkpoint(entry["content"])
        omitted.append(
            {
                "section": "context_entries.content",
                "reason": "compacted_to_checkpoint",
                "omitted_count": len(references),
                "references": references,
            }
        )

    def _compact_artifacts(
        self, payload: dict[str, Any], omitted: list[dict[str, Any]], token_budget: int
    ) -> None:
        """Keep artifact identity and finding state; shorten only their narration."""
        candidate = payload.get("candidate")
        if candidate and candidate.get("developer_claims"):
            omitted.append(
                {
                    "section": "candidate.developer_claims",
                    "reason": "compacted_to_reference",
                    "omitted_count": len(candidate["developer_claims"]),
                    "references": [
                        f"ctx:candidate:{candidate['candidate_id']}:developer_claims"
                    ],
                }
            )
            candidate["developer_claims"] = []

        references = [
            f"ctx:finding:{finding['finding_id']}"
            for finding in payload.get("known_failures") or []
            if len(finding["observation"]) > _CHECKPOINT_CHARS
        ]
        if not references:
            return
        for finding in payload["known_failures"]:
            finding["observation"] = _checkpoint(finding["observation"])
        omitted.append(
            {
                "section": "known_failures.observation",
                "reason": "compacted_to_checkpoint",
                "omitted_count": len(references),
                "references": references,
            }
        )

    def _compact_feature_rollup(
        self, payload: dict[str, Any], omitted: list[dict[str, Any]], token_budget: int
    ) -> None:
        """Reduce feature tickets to a rollup row plus a typed reference each."""
        tickets = payload.get("tickets") or []
        if not tickets:
            return
        payload["tickets"] = [
            {
                "ticket_id": ticket["ticket_id"],
                "title": ticket["title"],
                "status": ticket["status"],
                "priority": ticket["priority"],
                "required_role": ticket["required_role"],
                "dependency_ids": ticket["dependency_ids"],
            }
            for ticket in tickets
        ]
        omitted.append(
            {
                "section": "tickets.detail",
                "reason": "compacted_to_rollup",
                "omitted_count": len(tickets),
                "references": [f"ctx:ticket:{ticket['ticket_id']}" for ticket in tickets],
            }
        )

    def _drop_verbose_entries(
        self, payload: dict[str, Any], omitted: list[dict[str, Any]], token_budget: int
    ) -> None:
        """Drop the oldest narration first; durable decisions always stay."""
        entries = payload.get("context_entries")
        if not entries:
            return
        # Attached before dropping so each reference it keeps is itself measured;
        # otherwise the packet is refitted and then grown past its budget again.
        record: dict[str, Any] = {
            "section": "context_entries",
            "reason": "token_budget",
            "omitted_count": 0,
            "references": [],
        }
        omitted.append(record)
        while _estimate_tokens(payload) > token_budget:
            index = next(
                (
                    position
                    for position, entry in enumerate(entries)
                    if entry["entry_type"] in _VERBOSE_ENTRY_TYPES
                ),
                None,
            )
            if index is None:
                break
            record["references"].append(f"ctx:entry:{entries.pop(index)['entry_id']}")
            record["omitted_count"] += 1
        if not record["references"]:
            omitted.remove(record)
