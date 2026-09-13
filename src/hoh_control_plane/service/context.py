from __future__ import annotations

import hashlib

from hoh_control_plane.service._common import (
    Actor,
    Any,
    CLOSED_TICKET_STATUSES,
    ContextStaleError,
    Role,
    TicketStatus,
    _CHECKPOINT_CHARS,
    _FALLBACK_SECTION_PRIORITY,
    _PROTECTED_SECTIONS,
    _ROLE_REQUIRED_SECTIONS,
    _TICKET_SECTION_PRIORITY,
    _MAX_INHERITED_CONSTRAINTS,
    _VERBOSE_ENTRY_TYPES,
    _checkpoint,
    _estimate_tokens,
    _iso,
    _row,
    _snapshot_id,
    Connection,
    json,
    _serialize,
    _validated_budget,
    require_project,
)


# A ticket packet already names its own ticket, project and candidate, so every
# nested row that repeats them spends budget to say what the envelope already
# said. superseded_by is always NULL because the query selects only live entries.
# report_path is the one exception that is not merely redundant: it is an
# absolute path on whichever machine ran the tests, so it is unusable to any
# other agent, while storage_ref is the portable reference to the same report.
_REDUNDANT_SECTION_FIELDS: dict[str, tuple[str, ...]] = {
    "ticket": ("created_at", "updated_at"),
    "context_entries": ("project_id", "feature_id", "ticket_id", "superseded_by"),
    "candidate": ("ticket_id",),
    "worktree": ("ticket_id", "project_id"),
    "test_runs": ("ticket_id", "candidate_id", "report_path"),
    "known_failures": ("candidate_id",),
    "evidence": ("candidate_id",),
}

# An execution role does not plan the feature, it implements or verifies one
# ticket inside it, so the feature is kept as intent rather than as planning
# bookkeeping. A Planner keeps the whole row because the feature is its subject.
_EXECUTION_FEATURE_FIELDS = ("feature_id", "title", "objective", "status", "version")

# Section 7.2 gives a Developer known failures and their reproduction evidence,
# not QA's provenance machinery. The outcome plus a resolvable reference is what
# a Developer acts on; command digests and checksums are how QA proves identity.
_DEVELOPER_TEST_RUN_FIELDS = (
    "test_run_id",
    "exit_code",
    "timed_out",
    "duration_ms",
    "storage_ref",
    "created_at",
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
            ungated = {row[0] for row in connection.execute(
                "SELECT t.ticket_id FROM tickets t JOIN audit_events e ON e.aggregate_id = t.ticket_id "
                "WHERE t.feature_id = ? AND e.event_type = 'integration.landed_ungated'",
                (feature_id,)).fetchall()}
            verified = [
                constraint
                for ticket in tickets
                if ticket["status"] in {TicketStatus.VERIFIED.value, TicketStatus.DONE.value}
                and ticket["ticket_id"] not in ungated
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
                    WHERE (ticket_id = ? OR (ticket_id IS NULL AND feature_id = ?)) AND superseded_by IS NULL
                    ORDER BY created_at ASC
                    """,
                    (ticket_id, ticket["feature_id"]),
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
        inherited = self._inherited_constraints(ticket_id, ticket)
        experience = self._relevant_experience(project_id, ticket)
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
            "inherited_constraints": inherited,
            "experience": experience,
        }
        decisions = [entry for entry in contexts if entry["actor_role"] in {"planner", "admin"}
                     and entry["entry_type"] in {"planner_decision", "planning_decision", "design_decision", "risk", "note"}]
        latest = decisions[-1] if decisions else None
        payload["latest_planner_decision"] = ({"entry_id": latest["entry_id"],
            "context_ref": "ctx:entry:" + latest["entry_id"], "summary": _checkpoint(latest["content"])} if latest else None)
        source_versions = {
            "planning_revision": self._planning_revision(contexts),
            "ticket_version": ticket["version"],
            "feature_version": feature["version"],
            "candidate_commit": candidate["commit_sha"] if candidate else None,
            "dependencies": {
                dependency["ticket_id"]: dependency["status"] for dependency in dependencies
            },
        }
        self._project_role_sections(payload, actor)
        compiled = self._compile_context(
            payload,
            token_budget=budget,
            priority=_TICKET_SECTION_PRIORITY.get(actor.role, _FALLBACK_SECTION_PRIORITY),
            reference_prefix=f"ctx:ticket:{ticket_id}",
            compactors=(self._compact_history, self._compact_artifacts),
            protected=_PROTECTED_SECTIONS
            | _ROLE_REQUIRED_SECTIONS.get(actor.role, frozenset()),
        )
        self._record_context_snapshot(
            compiled["context_snapshot_id"],
            project_id=project_id,
            subject_type="ticket",
            subject_id=ticket_id,
            role=actor.role.value,
            source_versions=source_versions,
            generated_at=compiled["generated_at"],
        )
        return compiled

    def _inherited_constraints(
        self, ticket_id: str, ticket: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """What this feature has already proved, which this ticket must not break.

        Computed when the context is read rather than stored when the ticket was
        created, so behaviour verified after the fact still reaches work that is
        already in flight. Kept apart from the Planner's own constraints because
        the two are different in kind: these came from evidence, those from
        judgement, and a reader should be able to tell which is which.
        """
        own = {str(constraint).strip() for constraint in ticket.get("preservation_constraints") or []}
        seen: set[str] = set()
        inherited: list[dict[str, Any]] = []
        with self.store.connection() as connection:
            rows = connection.execute(
                """
                SELECT ticket_id, acceptance_criteria FROM tickets
                WHERE feature_id = ? AND ticket_id != ? AND status IN (?, ?)
                AND NOT EXISTS (SELECT 1 FROM audit_events e WHERE e.aggregate_id = tickets.ticket_id
                                AND e.event_type = 'integration.landed_ungated')
                ORDER BY updated_at DESC
                """,
                (
                    ticket["feature_id"],
                    ticket_id,
                    TicketStatus.VERIFIED.value,
                    TicketStatus.DONE.value,
                ),
            ).fetchall()
        for row in rows:
            record = _row(row)
            assert record is not None
            for criterion in record["acceptance_criteria"] or []:
                behaviour = str(criterion).strip()
                # Already stated by the Planner, or already inherited from an
                # earlier ticket: saying it twice does not make it truer.
                if not behaviour or behaviour in own or behaviour in seen:
                    continue
                seen.add(behaviour)
                inherited.append({"behaviour": behaviour, "verified_by": record["ticket_id"]})
                if len(inherited) >= _MAX_INHERITED_CONSTRAINTS:
                    return inherited
        return inherited

    def _record_context_snapshot(
        self,
        snapshot_id: str,
        *,
        project_id: str,
        subject_type: str,
        subject_id: str,
        role: str,
        source_versions: dict[str, Any],
        generated_at: str,
    ) -> None:
        """Persist what a reader observed, so a later write can be checked against it.

        The identifier is derived from the packet's own content, so identical
        state compiles to an identical row and re-reading unchanged context
        writes nothing new.
        """
        with self.store.connection(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO context_snapshots
                    (snapshot_id, project_id, subject_type, subject_id, role,
                     source_versions, generated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(snapshot_id) DO NOTHING
                """,
                (
                    snapshot_id,
                    project_id,
                    subject_type,
                    subject_id,
                    role,
                    _serialize(source_versions),
                    generated_at,
                ),
            )

    def require_fresh_context(
        self,
        connection: Connection,
        ticket_id: str,
        snapshot_id: str | None,
    ) -> None:
        """Refuse a consequential write decided from context that has since moved.

        Supplying a snapshot is optional: a caller that never read context cannot
        be stale against it. Supplying one it cannot produce, or one taken
        against a different ticket, is refused rather than ignored — silently
        accepting an unknown identifier would make the check decorative.
        """
        if snapshot_id is None:
            return
        recorded = _row(
            connection.execute(
                "SELECT * FROM context_snapshots WHERE snapshot_id = ?", (snapshot_id,)
            ).fetchone()
        )
        if recorded is None or recorded["subject_id"] != ticket_id:
            raise ContextStaleError(
                "The referenced context snapshot is not one this ticket produced",
                {"stale": ["snapshot"], "ticket_id": ticket_id},
            )
        observed = json.loads(recorded["source_versions"])
        ticket = self._ticket(connection, ticket_id)
        feature = self._one(
            connection,
            "SELECT * FROM features WHERE feature_id = ?",
            (ticket["feature_id"],),
        )
        candidate = _row(
            connection.execute(
                """
                SELECT commit_sha FROM candidates WHERE ticket_id = ?
                ORDER BY submitted_at DESC LIMIT 1
                """,
                (ticket_id,),
            ).fetchone()
        )
        dependencies = {
            row["ticket_id"]: row["status"]
            for row in connection.execute(
                """
                SELECT dependency.ticket_id, dependency.status
                FROM ticket_dependencies d
                JOIN tickets dependency ON dependency.ticket_id = d.depends_on_ticket_id
                WHERE d.ticket_id = ?
                """,
                (ticket_id,),
            ).fetchall()
        }
        stale: list[str] = []
        decisions = [dict(row) for row in connection.execute(
            "SELECT * FROM context_entries WHERE (ticket_id = ? OR (ticket_id IS NULL AND feature_id = ?)) AND superseded_by IS NULL ORDER BY created_at ASC",
            (ticket_id, ticket["feature_id"])).fetchall()]
        # Old snapshots did not pin planning decisions. Refresh once at upgrade
        # instead of allowing a pre-upgrade view to authorize current work.
        if observed.get("planning_revision") != self._planning_revision(decisions):
            stale.append("planning_decisions")
        if observed.get("ticket_version") != ticket["version"]:
            stale.append("ticket")
        if observed.get("feature_version") != feature["version"]:
            stale.append("feature")
        if observed.get("candidate_commit") != (candidate["commit_sha"] if candidate else None):
            stale.append("candidate")
        if observed.get("dependencies") != dependencies:
            stale.append("dependencies")
        if stale:
            # Name the sections rather than the values: which part to refresh is
            # what the caller needs, and the values themselves are what it will
            # get by refreshing.
            raise ContextStaleError(
                "Refresh this ticket's context before writing: " + ", ".join(stale),
                {"stale": stale, "ticket_id": ticket_id},
            )

    @staticmethod
    def _planning_revision(entries):
        material = [(e["entry_id"], e["entry_type"], e["content"], e["source_ref"]) for e in entries
                    if e["actor_role"] in {"planner", "admin"}]
        return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()

    @staticmethod
    def _project_role_sections(payload: dict[str, Any], actor: Actor) -> None:
        """Drop what the envelope already says, and what this role never acts on.

        This runs before budget fitting so compaction spends the budget on
        material a role reasons with, rather than on bookkeeping it cannot use.
        It removes fields, never sections: dropping a whole section is the
        budget-driven decision recorded in omitted_sections, and every field
        section 7.2 and 7.3 make a role's own is kept regardless of budget.
        """

        def strip(row: Any, fields: tuple[str, ...]) -> Any:
            if not isinstance(row, dict):
                return row
            return {key: value for key, value in row.items() if key not in fields}

        for section, fields in _REDUNDANT_SECTION_FIELDS.items():
            value = payload.get(section)
            if isinstance(value, list):
                payload[section] = [strip(item, fields) for item in value]
            elif value is not None:
                payload[section] = strip(value, fields)

        for finding in payload.get("known_failures") or []:
            if isinstance(finding, dict) and isinstance(finding.get("evidence"), list):
                finding["evidence"] = [
                    strip(item, _REDUNDANT_SECTION_FIELDS["evidence"])
                    for item in finding["evidence"]
                ]

        if actor.role is not Role.PLANNER and isinstance(payload.get("feature"), dict):
            payload["feature"] = {
                key: value
                for key, value in payload["feature"].items()
                if key in _EXECUTION_FEATURE_FIELDS
            }

        if actor.role is Role.DEVELOPER:
            payload["test_runs"] = [
                {
                    key: value
                    for key, value in run.items()
                    if key in _DEVELOPER_TEST_RUN_FIELDS
                }
                for run in payload.get("test_runs") or []
                if isinstance(run, dict)
            ]

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
