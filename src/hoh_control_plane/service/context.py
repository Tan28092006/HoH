from __future__ import annotations

import hashlib
import re

from hoh_control_plane.service._common import (
    Actor,
    Any,
    AuthorizationError,
    CLOSED_TICKET_STATUSES,
    ContextStaleError,
    DEFAULT_TOKEN_BUDGET,
    MAX_TOKEN_BUDGET,
    NotFoundError,
    Role,
    ValidationError,
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
    _SNAPSHOT_PLACEHOLDER,
    _id,
    _snapshot_digest,
    Connection,
    json,
    _serialize,
    agent_facing,
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
    "feature": ("project_id", "created_at", "updated_at"),
    "ticket": ("created_at", "updated_at"),
    "context_entries": ("project_id", "superseded_by", "actor_id", "created_at"),
    "candidate": ("ticket_id", "submitted_at", "frozen_at"),
    "worktree": (
        "ticket_id", "project_id", "created_at", "last_verified_at"
    ),
    "test_runs": ("ticket_id", "candidate_id", "report_path", "created_at"),
    "known_failures": ("candidate_id", "created_at"),
    "evidence": ("candidate_id", "created_at"),
}

# An execution role does not plan the feature, it implements or verifies one
# ticket inside it, so the feature is kept as intent rather than as planning
# bookkeeping. A Planner keeps the whole row because the feature is its subject.
_EXECUTION_FEATURE_FIELDS = ("feature_id", "title", "objective", "status", "version")
_TICKET_SUMMARY_FIELDS = (
    "ticket_id", "feature_id", "title", "status", "priority", "required_role",
    "version", "dependency_ids",
)
_VERSION_TOKEN = re.compile(r"v[0-9]+")

# Who wrote each section, so a reader can tell direction from evidence. Planner
# text sets the work; developer, qa and integrator text is advisory, a claim or
# an observation to check rather than an instruction; system is server state.
# Context entries and lessons carry their own `source`, taken from the role
# that wrote them. The packet lists only the sections it actually carries.
_SECTION_SOURCES: dict[str, str] = {
    "repository_state": "system",
    "planning_barrier": "system",
    "retirement": "planner",
    "verification_profile": "planner",
    "feature": "planner",
    "ticket": "planner",
    "tickets": "planner",
    "dependencies": "planner",
    "candidate": "developer",
    "worktree": "system",
    "test_runs": "qa",
    "known_failures": "qa",
    "evidence_bundle": "qa",
    "inherited_constraints": "system",
    "verified_behaviors": "system",
}
_AUTHOR_ROLES = frozenset({"planner", "developer", "qa", "integrator"})


def _source(role: Any) -> str:
    """The author label for a stored actor role; anything else is the server."""
    return str(role) if role in _AUTHOR_ROLES else "system"


def _section_sources(payload: dict[str, Any]) -> dict[str, list[str]]:
    """Group the carried, non-empty sections by author, in a fixed order."""
    grouped: dict[str, list[str]] = {}
    for section, source in _SECTION_SOURCES.items():
        if payload.get(section):
            grouped.setdefault(source, []).append(section)
    return grouped
_PACKET_ENVELOPE_KEYS = frozenset(
    {"context_snapshot_id", "estimated_tokens", "token_budget", "generated_at", "omitted_sections",
     "sources"}
)

# Section 7.2 gives a Developer known failures and their reproduction evidence,
# not QA's provenance machinery. The outcome plus a resolvable reference is what
# a Developer acts on; command digests and checksums are how QA proves identity.
_DEVELOPER_TEST_RUN_FIELDS = (
    "test_run_id",
    "exit_code",
    "timed_out",
    "duration_ms",
    "storage_ref",
)

# Field-level role projection is an information budget as well as RBAC. Every
# execution role receives the observable contract it acts on; planning metadata
# and edit scope are not repeated to roles that cannot mutate them. A contracted
# ticket already repeats each acceptance criterion inside validation_contract,
# so the older list is retained only for a legacy ticket without a contract.
_ROLE_TICKET_FIELDS: dict[Role, frozenset[str]] = {
    Role.DEVELOPER: frozenset(
        {
            "ticket_id", "feature_id", "ticket_type", "title", "objective",
            "status", "required_role", "validation_contract",
            "preservation_constraints", "file_scope", "blocked_reason", "version",
        }
    ),
    Role.QA: frozenset(
        {
            "ticket_id", "feature_id", "title", "objective", "status",
            "validation_contract", "preservation_constraints", "blocked_reason",
            "version",
        }
    ),
    Role.INTEGRATOR: frozenset(
        {
            "ticket_id", "feature_id", "title", "objective", "status",
            "validation_contract", "preservation_constraints", "blocked_reason",
            "version",
        }
    ),
}

# Machine narration and test-run echoes have dedicated durable sections. The
# remaining entry types are the decisions each role can use. Normal packets keep
# only the newest checkpoint per feature/ticket scope and type (up to three open
# risks/failures); Planner can explicitly request its older decision history.
# A history page is a list of checkpoints, each resolvable on its own; the page
# is bounded so that "show me the history" never becomes the whole log again.
_HISTORY_PAGE = 20

_ROLE_CONTEXT_ENTRY_TYPES: dict[Role, frozenset[str]] = {
    Role.DEVELOPER: frozenset(
        {
            "planner_decision", "design_decision", "risk", "developer_blocker",
            "known_failure", "write_scope_updated", "ticket_fields_updated",
            "worktree_handoff", "worktree_recovery", "integration_base_drift",
            "integration_regression_failed", "bootstrap_integration", "note",
        }
    ),
    Role.QA: frozenset(
        {
            "planner_decision", "design_decision", "risk", "known_failure",
            "ticket_fields_updated", "integration_base_drift",
            "integration_regression_failed", "note",
        }
    ),
    Role.INTEGRATOR: frozenset(
        {
            "planner_decision", "design_decision", "risk", "known_failure",
            "integration_base_drift", "integration_regression_failed",
            "integration_authorization", "bootstrap_integration", "note",
        }
    ),
    Role.PLANNER: frozenset(
        {
            "planner_decision", "design_decision", "risk", "developer_blocker",
            "known_failure", "write_scope_updated", "ticket_fields_updated",
            "worktree_recovery", "integration_base_drift", "integration_regression_failed",
            "integration_authorization", "bootstrap_integration", "note",
        }
    ),
}


class ContextMixin:
    """Role-bounded context packet compilation and compaction."""

    def get_feature_context(
        self,
        actor: Actor,
        feature_ref: str,
        token_budget: int | None = None,
        *,
        _full_budget: bool = False,
    ) -> dict[str, Any]:
        budget = min(_validated_budget(token_budget), DEFAULT_TOKEN_BUDGET)
        if _full_budget:
            budget = MAX_TOKEN_BUDGET
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
        compiled = self._compile_context(
            payload,
            token_budget=budget,
            priority=("tickets", "verified_behaviors"),
            reference_prefix=f"ctx:feature:{feature_id}",
            compactors=(self._compact_feature_rollup,),
            protected=frozenset({"feature"}),
        )
        compiled["context_snapshot_id"] = self._record_context_snapshot(
            compiled,
            project_id=feature["project_id"],
            subject_type="feature",
            subject_id=feature_id,
            role=actor.role.value,
            source_versions={"feature_version": feature["version"]},
        )
        return compiled

    def get_ticket_context(
        self,
        actor: Actor,
        ticket_ref: str,
        token_budget: int | None = None,
        include_history: bool = False,
        known_snapshot_id: str | None = None,
        *,
        _full_budget: bool = False,
    ) -> dict[str, Any]:
        """Compile the role's packet; `{unchanged, context_snapshot_id}` if the caller has it."""
        if known_snapshot_id is not None and not isinstance(known_snapshot_id, str):
            raise ValidationError("known_snapshot_id must be a string")
        requested_budget = _validated_budget(token_budget)
        if not isinstance(include_history, bool):
            raise ValidationError("include_history must be a boolean")
        if include_history and actor.role not in {Role.PLANNER, Role.ADMIN}:
            raise AuthorizationError(
                "Only Planner or Admin may expand ticket decision history"
            )
        budget = (
            requested_budget
            if include_history
            else min(requested_budget, DEFAULT_TOKEN_BUDGET)
        )
        if _full_budget:
            budget = MAX_TOKEN_BUDGET
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
            project = self._one(
                connection, "SELECT * FROM projects WHERE project_id = ?", (project_id,)
            )
            repository = _row(
                connection.execute(
                    "SELECT * FROM project_repositories WHERE project_id = ?", (project_id,)
                ).fetchone()
            )
            planning_barrier = self._repository_reconciliation_state(
                connection, project_id, ticket_id
            )
            retirement = _row(connection.execute(
                """
                SELECT * FROM ticket_retirement_requests
                WHERE ticket_id = ? ORDER BY requested_at DESC LIMIT 1
                """,
                (ticket_id,),
            ).fetchone())
            verification_profile = project.get("verification_profile") or {
                "schema_version": "1", "quality_policy": "legacy", "capabilities": []
            }
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
            evidence_bundle: dict[str, Any] | None = None
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
                    WHERE candidate_id = ?
                    ORDER BY created_at ASC
                    """,
                    (candidate["candidate_id"],),
                ).fetchall()
                all_findings: list[dict[str, Any]] = []
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
                    all_findings.append(finding)
                    if finding["status"] != "verified":
                        failures.append(finding)
                records = [
                    {
                        "finding_id": finding["finding_id"],
                        "criterion": finding["criterion"],
                        "status": finding["status"],
                        "observation": finding["observation"],
                        "evidence_refs": finding["evidence_refs"],
                        "scenario_refs": sorted({
                            item["scenario_ref"] for item in finding["evidence"]
                            if item and item.get("scenario_ref")
                        }),
                        "evidence_types": sorted({
                            item["evidence_type"] for item in finding["evidence"] if item
                        }),
                    }
                    for finding in all_findings
                ]
                evidence_bundle = {
                    "schema_version": "1",
                    "candidate_id": candidate["candidate_id"],
                    "candidate_commit": candidate["commit_sha"],
                    "verified_records": [r for r in records if r["status"] == "verified"],
                    "gap_records": [r for r in records if r["status"] != "verified"],
                }
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
            "repository_state": self._repository_revision(
                repository,
                self._relevant_branch_refs(candidate, worktree),
            ),
            "planning_barrier": planning_barrier,
            "retirement": (
                {
                    "request_id": retirement["request_id"],
                    "status": retirement["status"],
                    "target_status": retirement["target_status"],
                    "replacement_ticket_id": retirement.get("replacement_ticket_id"),
                    "reason": _checkpoint(retirement["reason"]),
                    "outcome": _checkpoint(retirement["outcome"])
                    if retirement.get("outcome") else None,
                }
                if retirement else None
            ),
            "verification_profile": verification_profile,
            "feature": feature,
            "ticket": ticket,
            "dependencies": dependencies,
            "context_entries": contexts,
            "candidate": candidate,
            "worktree": worktree,
            "test_runs": test_runs,
            "known_failures": failures,
            "evidence_bundle": evidence_bundle,
            "inherited_constraints": inherited,
            "experience": experience,
        }
        source_versions = {
            "planning_revision": self._planning_revision(contexts),
            "repository_revision": self._repository_revision_digest(
                repository,
                self._relevant_branch_refs(candidate, worktree),
            ),
            "repository_reconciliation_refs": [
                item["observation_ref"] for item in planning_barrier["pending"]
            ],
            "retirement": (
                {
                    "request_id": retirement["request_id"],
                    "status": retirement["status"],
                    "target_status": retirement["target_status"],
                }
                if retirement else None
            ),
            "ticket_version": ticket["version"],
            "feature_version": feature["version"],
            "candidate_commit": candidate["commit_sha"] if candidate else None,
            "dependencies": {
                dependency["ticket_id"]: dependency["status"] for dependency in dependencies
            },
        }
        self._project_role_sections(payload, actor, include_history=include_history)
        compiled = self._compile_context(
            payload,
            token_budget=budget,
            priority=_TICKET_SECTION_PRIORITY.get(actor.role, _FALLBACK_SECTION_PRIORITY),
            reference_prefix=f"ctx:ticket:{ticket_id}",
            compactors=(self._compact_history, self._compact_artifacts),
            protected=_PROTECTED_SECTIONS
            | _ROLE_REQUIRED_SECTIONS.get(actor.role, frozenset()),
        )
        compiled["context_snapshot_id"] = self._record_context_snapshot(
            compiled,
            project_id=project_id,
            subject_type="ticket",
            subject_id=ticket_id,
            role=actor.role.value,
            source_versions=source_versions,
        )
        if known_snapshot_id and compiled["context_snapshot_id"] == known_snapshot_id:
            return {"unchanged": True, "context_snapshot_id": known_snapshot_id}
        return compiled

    def resolve_context_reference(self, actor: Actor, reference: str) -> dict[str, Any]:
        """Resolve one typed reference a packet cited, under the packet's own rules.

        A compacted packet says what it left out and names each omission by a
        reference. Resolving one costs the caller only that item, which is the
        point of citing rather than inlining. Visibility follows the packet: an
        entry type a role never sees in a packet is not visible here either,
        and a project the actor is not scoped to is not found.
        """
        if not isinstance(reference, str) or not reference.startswith("ctx:"):
            raise ValidationError("reference must be a typed ctx: reference from a packet")
        parts = reference.split(":")
        kind = parts[1] if len(parts) > 2 else ""
        with self.store.connection() as connection:
            if kind == "entry" and len(parts) == 3:
                return self._resolve_entry(connection, actor, parts[2])
            if kind == "candidate" and len(parts) == 4 and parts[3] in {
                "developer_claims", "changed_paths"
            }:
                return self._resolve_candidate_section(connection, actor, parts[2], parts[3])
            if kind == "finding" and len(parts) == 3:
                return self._resolve_finding(connection, actor, parts[2])
            rest = parts[3:]
            if rest and _VERSION_TOKEN.fullmatch(rest[0]):
                rest = rest[1:]
            if kind == "ticket" and rest == ["history"]:
                return self._resolve_ticket_history(connection, actor, parts[2])
            if kind == "ticket" and not rest:
                return self._resolve_ticket_summary(connection, actor, parts[2], reference)
            if kind == "feature" and not rest:
                return self._resolve_feature_summary(connection, actor, parts[2], reference)
        # A section the packet dropped for its budget: recompile without the
        # budget, under the same role projection, and return only that section.
        if kind in {"ticket", "feature"} and len(rest) == 1:
            if kind == "ticket":
                packet = self.get_ticket_context(actor, parts[2], _full_budget=True)
            else:
                packet = self.get_feature_context(actor, parts[2], _full_budget=True)
            if rest[0] in packet and rest[0] not in _PACKET_ENVELOPE_KEYS:
                return {"reference": reference, rest[0]: packet[rest[0]]}
        raise ValidationError(
            "Unsupported reference; resolvable forms are ctx:entry:<id>, "
            "ctx:candidate:<id>:developer_claims, ctx:candidate:<id>:changed_paths, "
            "ctx:finding:<id>, ctx:ticket:<id>[:v<n>][:<section>|:history] and "
            "ctx:feature:<id>[:v<n>][:<section>]"
        )

    def _resolve_ticket_summary(
        self, connection: Connection, actor: Actor, ticket_id: str, reference: str
    ) -> dict[str, Any]:
        ticket, _project_id = self._authorized_ticket(connection, actor, ticket_id)
        return {
            "reference": reference,
            "ticket": {key: ticket.get(key) for key in _TICKET_SUMMARY_FIELDS},
            "next_step": "call ticket.get_context for the full packet",
        }

    def _resolve_feature_summary(
        self, connection: Connection, actor: Actor, feature_id: str, reference: str
    ) -> dict[str, Any]:
        feature = self._one(
            connection, "SELECT * FROM features WHERE feature_id = ?", (feature_id,)
        )
        require_project(actor, feature["project_id"])
        return {
            "reference": reference,
            "feature": {key: feature.get(key) for key in _EXECUTION_FEATURE_FIELDS},
            "next_step": "call feature.get_context for the full packet",
        }

    def _resolve_entry(
        self, connection: Connection, actor: Actor, entry_id: str
    ) -> dict[str, Any]:
        entry = _row(connection.execute(
            "SELECT * FROM context_entries WHERE entry_id = ?", (entry_id,)
        ).fetchone())
        if entry is None:
            raise NotFoundError("Context entry not found")
        require_project(actor, entry["project_id"])
        allowed = _ROLE_CONTEXT_ENTRY_TYPES.get(actor.role)
        if allowed is not None and entry["entry_type"] not in allowed:
            raise AuthorizationError("This entry type is not part of your role's packet")
        return {
            "reference": f"ctx:entry:{entry_id}",
            "entry": {
                key: entry[key]
                for key in (
                    "entry_id", "entry_type", "content", "source_ref",
                    "artifact_commit", "actor_role", "ticket_id", "feature_id",
                    "created_at", "superseded_by",
                )
            },
        }

    def _resolve_candidate_section(
        self, connection: Connection, actor: Actor, candidate_id: str, section: str
    ) -> dict[str, Any]:
        candidate = _row(connection.execute(
            "SELECT * FROM candidates WHERE candidate_id = ?", (candidate_id,)
        ).fetchone())
        if candidate is None:
            raise NotFoundError("Candidate not found")
        self._authorized_ticket(connection, actor, candidate["ticket_id"])
        if section == "changed_paths":
            value: Any = self._candidate_with_changed_paths(connection, candidate)["changed_paths"]
        else:
            value = candidate.get("developer_claims") or []
        return {
            "reference": f"ctx:candidate:{candidate_id}:{section}",
            "candidate_id": candidate_id,
            "ticket_id": candidate["ticket_id"],
            "commit_sha": candidate["commit_sha"],
            section: value,
        }

    def _resolve_finding(
        self, connection: Connection, actor: Actor, finding_id: str
    ) -> dict[str, Any]:
        finding = _row(connection.execute(
            """
            SELECT f.*, c.ticket_id FROM qa_findings f
            JOIN candidates c ON c.candidate_id = f.candidate_id
            WHERE f.finding_id = ?
            """,
            (finding_id,),
        ).fetchone())
        if finding is None:
            raise NotFoundError("QA finding not found")
        self._authorized_ticket(connection, actor, finding["ticket_id"])
        return {
            "reference": f"ctx:finding:{finding_id}",
            "finding": {
                key: finding[key]
                for key in (
                    "finding_id", "candidate_id", "ticket_id", "criterion", "status",
                    "severity", "observation", "evidence_refs", "created_at",
                )
            },
        }

    def _resolve_ticket_history(
        self, connection: Connection, actor: Actor, ticket_id: str
    ) -> dict[str, Any]:
        # Execution roles see the history their packet cited, filtered to the
        # entry types their packet may carry (below).
        ticket, _project_id = self._authorized_ticket(connection, actor, ticket_id)
        rows = connection.execute(
            """
            SELECT entry_id, entry_type, content, source_ref, actor_role, created_at,
                   superseded_by
            FROM context_entries
            WHERE ticket_id = ? OR (ticket_id IS NULL AND feature_id = ?)
            ORDER BY created_at DESC LIMIT ?
            """,
            (ticket_id, ticket["feature_id"], _HISTORY_PAGE),
        ).fetchall()
        allowed = _ROLE_CONTEXT_ENTRY_TYPES.get(actor.role)
        entries = [
            {**_row(row), "content": _checkpoint(row["content"])}  # type: ignore[dict-item]
            for row in rows
            if allowed is None or row["entry_type"] in allowed
        ]
        return {
            "reference": f"ctx:ticket:{ticket_id}:v{ticket['version']}:history",
            "entries": entries,
            "next_step": "resolve ctx:entry:<id> for the full text of one entry",
        }

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
        compiled: dict[str, Any],
        *,
        project_id: str,
        subject_type: str,
        subject_id: str,
        role: str,
        source_versions: dict[str, Any],
    ) -> str:
        """Persist what a reader observed, so a later write can be checked against it.

        The row is found by the packet's content digest, so identical state
        compiles to the same row and re-reading unchanged context writes
        nothing new. The name returned is the row's short counter identifier,
        which is what the packet carries and what a writer quotes back.
        """
        digest = _snapshot_digest(compiled)
        with self.store.connection(immediate=True) as connection:
            existing = connection.execute(
                "SELECT snapshot_id FROM context_snapshots WHERE content_digest = ?",
                (digest,),
            ).fetchone()
            if existing is not None:
                return str(existing["snapshot_id"])
            snapshot_id = _id(connection, "ctxsnap")
            connection.execute(
                """
                INSERT INTO context_snapshots
                    (snapshot_id, project_id, subject_type, subject_id, role,
                     source_versions, generated_at, content_digest)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot_id,
                    project_id,
                    subject_type,
                    subject_id,
                    role,
                    _serialize(source_versions),
                    compiled["generated_at"],
                    digest,
                ),
            )
        return snapshot_id

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
                SELECT commit_sha, branch_ref FROM candidates WHERE ticket_id = ?
                ORDER BY submitted_at DESC LIMIT 1
                """,
                (ticket_id,),
            ).fetchone()
        )
        repository = _row(
            connection.execute(
                """SELECT r.* FROM project_repositories r
                   JOIN features f ON f.project_id = r.project_id
                   JOIN tickets t ON t.feature_id = f.feature_id
                   WHERE t.ticket_id = ?""",
                (ticket_id,),
            ).fetchone()
        )
        worktree = _row(
            connection.execute(
                "SELECT branch_ref FROM worktrees WHERE ticket_id = ?", (ticket_id,)
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
        if observed.get("repository_revision") != self._repository_revision_digest(
            repository, self._relevant_branch_refs(candidate, worktree)
        ):
            stale.append("repository")
        current_reconciliation_refs = [
            item["observation_ref"]
            for item in self._repository_reconciliation_state(
                connection, recorded["project_id"], ticket_id
            )["pending"]
        ]
        if observed.get("repository_reconciliation_refs", []) != current_reconciliation_refs:
            stale.append("planning_barrier")
        retirement = _row(connection.execute(
            """
            SELECT * FROM ticket_retirement_requests
            WHERE ticket_id = ? ORDER BY requested_at DESC LIMIT 1
            """,
            (ticket_id,),
        ).fetchone())
        current_retirement = (
            {
                "request_id": retirement["request_id"],
                "status": retirement["status"],
                "target_status": retirement["target_status"],
            }
            if retirement else None
        )
        if observed.get("retirement") != current_retirement:
            stale.append("retirement")
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
    def _repository_revision(repository, branch_refs: tuple[str, ...] = ()):
        if repository is None:
            return None
        revision = {
            "mainline_commit": repository.get("observed_mainline_commit"),
            "upstream_commit": repository.get("observed_upstream_commit"),
        }
        if branch_refs:
            remote_refs = json.loads(repository.get("observed_remote_refs") or "{}")
            relevant: dict[str, str | None] = {}
            for branch_ref in branch_refs:
                short = branch_ref.removeprefix("refs/heads/").removeprefix("refs/remotes/")
                exact = remote_refs.get(short)
                matches = sorted(
                    commit for name, commit in remote_refs.items()
                    if name.endswith("/" + short)
                )
                relevant[short] = exact if exact is not None else (matches[0] if matches else None)
            revision["relevant_branches"] = relevant
        return revision

    @classmethod
    def _repository_revision_digest(cls, repository, branch_refs: tuple[str, ...] = ()):
        revision = cls._repository_revision(repository, branch_refs)
        if revision is None:
            return None
        return hashlib.sha256(
            json.dumps(revision, sort_keys=True).encode()
        ).hexdigest()

    @staticmethod
    def _relevant_branch_refs(candidate, worktree) -> tuple[str, ...]:
        return tuple(sorted({
            row["branch_ref"]
            for row in (candidate, worktree)
            if row is not None and row.get("branch_ref")
        }))

    @staticmethod
    def _project_role_sections(
        payload: dict[str, Any], actor: Actor, *, include_history: bool
    ) -> None:
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

        ticket = payload.get("ticket")
        allowed_ticket_fields = _ROLE_TICKET_FIELDS.get(actor.role)
        if isinstance(ticket, dict) and ticket.get("validation_contract"):
            # Contract entries carry the exact criteria; the service verifies
            # one-to-one coverage when Planner configures the contract.
            ticket.pop("acceptance_criteria", None)
        if isinstance(ticket, dict) and allowed_ticket_fields is not None:
            if not ticket.get("validation_contract"):
                allowed_ticket_fields = allowed_ticket_fields | {"acceptance_criteria"}
            payload["ticket"] = {
                key: value for key, value in ticket.items()
                if key in allowed_ticket_fields
            }

        entries = payload.get("context_entries") or []
        allowed_entry_types = _ROLE_CONTEXT_ENTRY_TYPES.get(actor.role)
        if allowed_entry_types is not None:
            authorized = [
                entry for entry in entries
                if entry.get("entry_type") in allowed_entry_types
            ]
            removed = len(entries) - len(authorized)
            if not include_history:
                kept_reversed: list[dict[str, Any]] = []
                counts: dict[tuple[str, str], int] = {}
                for entry in reversed(authorized):
                    scope = "ticket" if entry.get("ticket_id") else "feature"
                    entry_type = str(entry.get("entry_type"))
                    key = (scope, entry_type)
                    limit = 3 if entry_type in {"risk", "known_failure"} else 1
                    if counts.get(key, 0) >= limit:
                        removed += 1
                        continue
                    counts[key] = counts.get(key, 0) + 1
                    kept_reversed.append(entry)
                authorized = list(reversed(kept_reversed))
            payload["context_entries"] = authorized
            if removed:
                payload.setdefault("_precompiled_omissions", []).append(
                    {
                        "section": "context_entries.history",
                        "reason": "role_current_view",
                        "omitted_count": removed,
                        "references": [payload["context_ref"] + ":history"],
                    }
                )
        for entry in payload.get("context_entries") or []:
            if isinstance(entry, dict):
                entry.pop("feature_id", None)
                entry.pop("ticket_id", None)
                entry["source"] = _source(entry.pop("actor_role", None))
        for note in payload.get("experience") or []:
            if isinstance(note, dict) and "recorded_by" in note:
                note["source"] = _source(note.pop("recorded_by"))

        if actor.role is not Role.PLANNER and isinstance(payload.get("feature"), dict):
            payload["feature"] = {
                key: value
                for key, value in payload["feature"].items()
                if key in _EXECUTION_FEATURE_FIELDS
            }

        if actor.role not in {Role.PLANNER, Role.QA, Role.ADMIN}:
            payload["verification_profile"] = None

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
        # Shortened before fitting, so the budget measures the SHAs as delivered.
        payload = agent_facing(payload)
        omitted: list[dict[str, Any]] = payload.pop("_precompiled_omissions", [])
        payload["token_budget"] = token_budget
        payload["omitted_sections"] = omitted
        payload["generated_at"] = _iso()
        # Same-width placeholders so the envelope is measured, not added after the
        # packet has already been fitted to its budget.
        payload["estimated_tokens"] = token_budget
        payload["context_snapshot_id"] = _SNAPSHOT_PLACEHOLDER
        payload["sources"] = _section_sources(payload)

        # These compactors remove duplicated narration and attach typed refs;
        # they are semantic projections, not merely overflow recovery.
        for compact in compactors:
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
        # A section dropped for the budget loses its label too; this only shrinks.
        payload["sources"] = _section_sources(payload)
        payload["estimated_tokens"] = _estimate_tokens(payload)
        return payload

    def _compact_history(
        self, payload: dict[str, Any], omitted: list[dict[str, Any]], token_budget: int
    ) -> None:
        """Shorten history to checkpoints while keeping resolvable entry references."""
        references = [
            f"ctx:entry:{entry['entry_id']}"
            for entry in payload.get("context_entries") or []
            if len(entry["content"]) > _CHECKPOINT_CHARS
        ]
        if not references:
            return
        for entry in payload["context_entries"]:
            if len(entry["content"]) > _CHECKPOINT_CHARS:
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
        ticket = payload.get("ticket")
        if (
            candidate
            and isinstance(candidate.get("changed_paths"), list)
            and isinstance(ticket, dict)
        ):
            # Submission already refused any path outside the write scope, so
            # the full list repeats file_scope one path at a time. A path that
            # is nevertheless outside scope (scope narrowed after submission) is
            # the one fact worth the room, so that case keeps the whole list.
            paths = candidate["changed_paths"]
            scope = self._ticket_file_scope(payload)
            outside = [
                path for path in paths if not self._path_is_in_any_scope(path, scope)
            ]
            if paths and not outside:
                candidate["changed_paths_summary"] = {
                    "count": len(paths),
                    "within_file_scope": True,
                }
                del candidate["changed_paths"]
                omitted.append(
                    {
                        "section": "candidate.changed_paths",
                        "reason": "compacted_to_reference",
                        "omitted_count": len(paths),
                        "references": [
                            f"ctx:candidate:{candidate['candidate_id']}:changed_paths"
                        ],
                    }
                )
        # QA is the evaluator of the Developer's work, so it gets the claims only
        # by reference: it observes first and compares afterwards, instead of
        # reading the maker's conclusions before running a single scenario.
        drop_claims = payload.get("role") in {
            Role.DEVELOPER.value, Role.QA.value, Role.INTEGRATOR.value
        } or _estimate_tokens(payload) > token_budget
        if candidate and candidate.get("developer_claims") and drop_claims:
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

    def _ticket_file_scope(self, payload: dict[str, Any]) -> list[str]:
        """The write scope as stored, even for a role whose projection hides it."""
        ticket = payload.get("ticket") or {}
        if isinstance(ticket.get("file_scope"), list):
            return list(ticket["file_scope"])
        with self.store.connection() as connection:
            row = connection.execute(
                "SELECT file_scope FROM tickets WHERE ticket_id = ?",
                (ticket.get("ticket_id"),),
            ).fetchone()
        stored = _row(row)
        return list(stored["file_scope"]) if stored and stored.get("file_scope") else []

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
        """Drop old history while retaining the newest checkpoint of each durable type."""
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
            # Narration is already represented by test runs/audit state and is the
            # first thing to go. If decisions alone still overflow the packet,
            # retain the newest checkpoint for every typed decision and replace
            # older instances with references. The finite vocabulary bounds the
            # remaining packet even on a long-running project.
            index = next(
                (
                    position
                    for position, entry in enumerate(entries)
                    if entry["entry_type"] in _VERBOSE_ENTRY_TYPES
                ),
                None,
            )
            if index is None:
                newest_by_type = {
                    entry["entry_type"]: entry["entry_id"] for entry in entries
                }
                index = next(
                    (
                        position
                        for position, entry in enumerate(entries)
                        if newest_by_type[entry["entry_type"]] != entry["entry_id"]
                    ),
                    None,
                )
            if index is None:
                break
            record["references"].append(f"ctx:entry:{entries.pop(index)['entry_id']}")
            record["omitted_count"] += 1
        if not record["references"]:
            omitted.remove(record)
