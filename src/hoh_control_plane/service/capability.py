"""Break the QA capability approval cycle with host-observed, lease-free evidence.

The cycle this replaces: QA needed a lease to produce evidence, a lease needed
an approved capability, and approval needed evidence. Nothing could start it,
so QA waited for Planner and Planner waited for QA. In production no QA agent,
in any project, had ever been approved for a single capability.

The way out is evidence that exists before any lease and that QA does not
author. The HoH host probes the equipment on the machine the QA agent says it
runs on, records the outcome against that agent and a fingerprint of that
machine, and a Planner approval is accepted only against such a record. When a
QA-ready candidate is blocked on approval alone, one Planner job names the
exact call that unblocks it, and approving closes that job.
"""
from __future__ import annotations

import hashlib

from hoh_control_plane.capability_probe import machine_fingerprint
from hoh_control_plane.service.planner_jobs import CAPABILITY_REVIEW_MARK
from hoh_control_plane.service._common import (
    Actor,
    Any,
    Connection,
    InterventionSeverity,
    PlannerJobType,
    Role,
    TicketStatus,
    ValidationError,
    _id,
    _iso,
    _row,
    json,
    require_project,
    require_role,
)

# A preflight is re-run only when what it depends on changes; otherwise the
# existing record is the evidence, and asking again returns it unchanged.
_PREFLIGHT_REF = "preflight:"
_REVIEW_MARK = CAPABILITY_REVIEW_MARK
# Wave 7A: the server approves what a host preflight proves; a configuration or
# declaration change revokes that approval until the next preflight.
HOST_PREFLIGHT_APPROVER = "system:host-preflight"
APPROVAL_REVOKED = "system:approval-revoked"
_SYSTEM = Actor(HOST_PREFLIGHT_APPROVER, Role.ADMIN, frozenset({"*"}))


class CapabilityMixin:
    """Lease-free capability evidence, strict approval and one review job."""

    # ------------------------------------------------------------ preflight

    def run_capability_preflight(self, actor: Actor, project_id: str) -> dict[str, Any]:
        """Have the HoH host observe this QA agent's declared equipment.

        Needs no ticket, claim or lease: this is the evidence a Planner approval
        rests on, so it has to exist before either. Idempotent: an unchanged
        machine, declaration and configuration return the existing record.
        """
        require_role(actor, Role.QA)
        require_project(actor, project_id)
        with self.store.connection() as connection:
            presence = self._one(
                connection, "SELECT * FROM agent_presence WHERE agent_id = ?", (actor.agent_id,)
            )
            if project_id not in set(presence.get("project_ids") or []):
                raise ValidationError("Register for this project before running a preflight")
            fingerprint = machine_fingerprint(presence.get("machine_id"))
            if fingerprint is None:
                raise ValidationError(
                    "Register a stable machine_id before running a capability preflight",
                    {"reason": "capability_machine_missing"},
                )
            project = self._one(
                connection, "SELECT * FROM projects WHERE project_id = ?", (project_id,)
            )
            profile = set((project.get("verification_profile") or {}).get("capabilities") or [])
            declared = set(presence.get("execution_capabilities") or [])
            requested = sorted(declared & profile)
            repository = _row(connection.execute(
                "SELECT repository_path FROM project_repositories WHERE project_id = ?",
                (project_id,),
            ).fetchone())
            test_profile = _row(connection.execute(
                "SELECT test_command FROM project_test_profiles WHERE project_id = ?",
                (project_id,),
            ).fetchone())
            configured = {
                row["capability"]: json.loads(row["command"])
                for row in connection.execute(
                    "SELECT capability, command FROM project_capability_preflights WHERE project_id = ?",
                    (project_id,),
                ).fetchall()
            }
        if not requested:
            raise ValidationError(
                "No declared capability is in this project's verification profile",
                {"reason": "nothing_to_preflight", "declared": sorted(declared),
                 "profile": sorted(profile)},
            )

        # Probes run outside any database transaction: they start processes.
        prober = self.capability_prober
        host_matches = (presence.get("machine_id") or "").strip().lower() in prober.host_names()
        outcomes: dict[str, dict[str, Any]] = {}
        for capability in requested:
            if not host_matches:
                outcomes[capability] = {
                    "proven": False,
                    "reason": "qa_machine_is_not_this_host",
                    "method": "none",
                    "duration_ms": 0,
                }
                continue
            outcomes[capability] = prober.probe(
                capability,
                repository_path=(repository or {}).get("repository_path"),
                test_command=(test_profile or {}).get("test_command"),
                configured_command=configured.get(capability),
            ).as_record()

        # Timing varies run to run; identity rests on what was proven and how.
        stable = {
            capability: {k: v for k, v in outcome.items() if k != "duration_ms"}
            for capability, outcome in outcomes.items()
        }
        record_digest = hashlib.sha256(json.dumps({
            "project_id": project_id,
            "agent_id": actor.agent_id,
            "machine": fingerprint,
            "host_matches": host_matches,
            "outcomes": stable,
        }, sort_keys=True).encode("utf-8")).hexdigest()

        with self.store.connection(immediate=True) as connection:
            existing = _row(connection.execute(
                "SELECT * FROM capability_preflights WHERE record_digest = ?", (record_digest,)
            ).fetchone())
            if existing is None:
                preflight_id = _id(connection, "preflight")
                connection.execute(
                    """
                    INSERT INTO capability_preflights
                        (preflight_id, project_id, agent_id, machine_fingerprint,
                         host_matches_machine, requested_capabilities, outcomes,
                         record_digest, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (preflight_id, project_id, actor.agent_id, fingerprint,
                     1 if host_matches else 0, json.dumps(requested),
                     json.dumps(outcomes, sort_keys=True), record_digest, _iso()),
                )
                self._event(
                    connection, actor, project_id, "qa.capability_preflight_recorded",
                    "agent_presence", actor.agent_id, 1,
                    {"preflight_ref": _PREFLIGHT_REF + preflight_id,
                     "proven": sorted(c for c, o in outcomes.items() if o["proven"])},
                )
                record = self._preflight(connection, preflight_id)
                reused = False
            else:
                record = self._preflight(connection, existing["preflight_id"])
                reused = True
            # The probes just ran on the agent's own machine, so the record is
            # current whether it is new or an identical earlier one.
            approval = self._approve_from_preflight(connection, project_id, record)
            jobs = self._open_capability_review_jobs(connection, actor, project_id, record)
        return {
            **self._preflight_summary(record), "reused": reused,
            "approved": approval["approved"],
            "resolved_planner_jobs": approval["resolved_planner_jobs"],
            "planner_jobs": jobs,
        }

    def configure_capability_preflight(
        self,
        actor: Actor,
        project_id: str,
        capability: str,
        command: list[str],
        reason: str,
    ) -> dict[str, Any]:
        """Set, or with an empty command remove, the preflight for one capability.

        For equipment no built-in probe can observe, such as a dataset fixture or
        a tracing runtime. The command runs on the HoH host in the repository;
        exit status zero proves the capability.
        """
        require_role(actor, Role.PLANNER, Role.ADMIN)
        require_project(actor, project_id)
        self._bounded_text(capability, "capability", 100)
        self._bounded_text(reason, "reason", 2000)
        if not isinstance(command, list) or not all(isinstance(part, str) and part for part in command):
            raise ValidationError("command must be a list of non-empty strings")
        self._bounded_list(command, "command", 40, 1000)
        with self.store.connection(immediate=True) as connection:
            project = self._one(
                connection, "SELECT * FROM projects WHERE project_id = ?", (project_id,)
            )
            profile = set((project.get("verification_profile") or {}).get("capabilities") or [])
            if capability not in profile:
                raise ValidationError(
                    "Capability is not in the project's verification profile",
                    {"reason": "verification_capabilities_missing", "capabilities": [capability]},
                )
            if command:
                connection.execute(
                    """
                    INSERT INTO project_capability_preflights
                        (project_id, capability, command, configured_by, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(project_id, capability) DO UPDATE SET
                        command = excluded.command, configured_by = excluded.configured_by,
                        updated_at = excluded.updated_at
                    """,
                    (project_id, capability, json.dumps(command), actor.agent_id, _iso()),
                )
            else:
                connection.execute(
                    "DELETE FROM project_capability_preflights WHERE project_id = ? AND capability = ?",
                    (project_id, capability),
                )
            self._event(
                connection, actor, project_id, "project.capability_preflight_configured",
                "project", project_id, project["version"],
                {"capability": capability, "configured": bool(command), "reason": reason},
            )
            self._revoke_host_preflight_approvals(
                connection, actor, project_id, "capability_preflight_configured"
            )
        return {"project_id": project_id, "capability": capability, "configured": bool(command)}

    # ------------------------------------------------------------- approval

    def _proven_by_preflight(
        self,
        connection: Connection,
        project_id: str,
        presence: dict[str, Any],
        evidence_refs: list[str],
    ) -> set[str]:
        """Capabilities the referenced host preflights prove for this agent and machine.

        Fails closed: a reference that is not a preflight record, or one taken
        for another project, agent or machine, refuses the whole approval.
        """
        fingerprint = machine_fingerprint(presence.get("machine_id"))
        proven: set[str] = set()
        for ref in evidence_refs:
            if not ref.startswith(_PREFLIGHT_REF):
                raise ValidationError(
                    "Capability approval accepts only host preflight records; run "
                    "qa.run_capability_preflight and cite the returned preflight_ref",
                    {"reason": "evidence_not_preflight", "evidence_ref": ref},
                )
            record = _row(connection.execute(
                "SELECT * FROM capability_preflights WHERE preflight_id = ?",
                (ref[len(_PREFLIGHT_REF):],),
            ).fetchone())
            if record is None:
                raise ValidationError("Unknown preflight record",
                                      {"reason": "evidence_unknown", "evidence_ref": ref})
            if record["project_id"] != project_id:
                raise ValidationError("Preflight was recorded for another project",
                                      {"reason": "evidence_other_project", "evidence_ref": ref})
            if record["agent_id"] != presence["agent_id"]:
                raise ValidationError("Preflight was recorded for another agent",
                                      {"reason": "evidence_other_agent", "evidence_ref": ref})
            if record["machine_fingerprint"] != fingerprint:
                raise ValidationError("Preflight was recorded on another machine",
                                      {"reason": "evidence_other_machine", "evidence_ref": ref})
            outcomes = json.loads(record["outcomes"])
            proven |= {capability for capability, outcome in outcomes.items() if outcome["proven"]}
        return proven

    def _resolve_capability_review_jobs(
        self, connection: Connection, actor: Actor, project_id: str, agent_id: str,
        approved: set[str], summary: str,
    ) -> list[str]:
        """Close review jobs for this agent whose blocked tickets approval now clears."""
        rows = connection.execute(
            """
            SELECT planner_job_id, ticket_id, evidence_refs FROM planner_jobs
            WHERE project_id = ? AND status IN ('READY', 'IN_PROGRESS')
            """,
            (project_id,),
        ).fetchall()
        resolved: list[str] = []
        timestamp = _iso()
        for row in rows:
            refs = json.loads(row["evidence_refs"])
            if _REVIEW_MARK + agent_id not in refs:
                continue
            ticket = self._ticket(connection, row["ticket_id"])
            if not self._required_execution_capabilities_of(ticket) <= approved:
                continue
            if ticket["status"] in {TicketStatus.BLOCKED.value, TicketStatus.REWORK.value}:
                # A failure holds this ticket; approval is not its resolution,
                # and closing the job would leave it with no wake-up (C2).
                continue
            connection.execute(
                """
                UPDATE planner_jobs
                SET status = 'RESOLVED', resolution = ?, resolved_by = ?, resolved_at = ?,
                    updated_at = ?
                WHERE planner_job_id = ? AND status IN ('READY', 'IN_PROGRESS')
                """,
                (summary, actor.agent_id, timestamp, timestamp, row["planner_job_id"]),
            )
            self._close_job_interventions(connection, [row["planner_job_id"]])
            connection.execute(
                "DELETE FROM planner_job_leases WHERE planner_job_id = ?", (row["planner_job_id"],)
            )
            self._event(
                connection, actor, project_id, "planner_job.resolved", "planner_job",
                row["planner_job_id"], 1, {"resolution": "capability_approved"},
            )
            resolved.append(row["planner_job_id"])
        return resolved

    def _approve_from_preflight(
        self, connection: Connection, project_id: str, record: dict[str, Any]
    ) -> dict[str, Any]:
        """Approve declared ∩ proven for the preflight's agent on its own machine.

        Planner's approval could only ever restate this set (``_proven_by_preflight``
        refuses anything else), so the server records it in the same transaction
        and the Planner step is left for overrides. Nothing is approved from a
        record taken on another host, for another machine than the one the agent
        is registered from now, or for a capability the agent did not declare.
        """
        presence = _row(connection.execute(
            "SELECT * FROM agent_presence WHERE agent_id = ?", (record["agent_id"],)
        ).fetchone())
        unchanged = {"approved": None, "resolved_planner_jobs": []}
        if presence is None or not record["host_matches_machine"]:
            return unchanged
        if record["machine_fingerprint"] != machine_fingerprint(presence.get("machine_id")):
            return unchanged
        project = self._one(
            connection, "SELECT * FROM projects WHERE project_id = ?", (project_id,)
        )
        profile = set((project.get("verification_profile") or {}).get("capabilities") or [])
        declared = set(presence.get("execution_capabilities") or [])
        approved = sorted(declared & set(record["proven"]) & profile)
        ref = _PREFLIGHT_REF + record["preflight_id"]
        current = (
            sorted(presence.get("approved_execution_capabilities") or []),
            presence.get("capability_approved_machine_id"),
            presence.get("capability_approved_by"),
        )
        if current != (approved, presence["machine_id"], HOST_PREFLIGHT_APPROVER):
            timestamp = _iso()
            connection.execute(
                """UPDATE agent_presence
                   SET approved_execution_capabilities = ?, capability_approval_refs = ?,
                       capability_approved_machine_id = ?, capability_approved_by = ?,
                       capability_approved_at = ?, updated_at = ?
                   WHERE agent_id = ?""",
                (
                    json.dumps(approved), json.dumps([ref]), presence["machine_id"],
                    HOST_PREFLIGHT_APPROVER, timestamp, timestamp, presence["agent_id"],
                ),
            )
            self._event(
                connection, _SYSTEM, project_id, "qa.capability_auto_approved",
                "agent_presence", presence["agent_id"], 1,
                {"capabilities": approved, "preflight_ref": ref,
                 "decided_by": HOST_PREFLIGHT_APPROVER},
            )
        resolved = self._resolve_capability_review_jobs(
            connection, _SYSTEM, project_id, presence["agent_id"], set(approved),
            f"Capability approved by the server from host preflight {ref}.",
        )
        return {"approved": approved, "resolved_planner_jobs": resolved}

    def _revoke_qa_machine_approvals(
        self, connection: Connection, actor: Actor, project_id: str, fingerprint: str,
    ) -> list[str]:
        """Withdraw every QA approval held on one machine after it failed (review C3).

        Whoever approved it, the approval was for equipment on a machine that
        has just failed as infrastructure. Its QA agents rerun the preflight
        once the machine is repaired and its Planner job is resolved.
        """
        revoked: list[str] = []
        timestamp = _iso()
        for row in connection.execute(
            "SELECT agent_id, project_ids, machine_id, approved_execution_capabilities "
            "FROM agent_presence WHERE role = ?",
            (Role.QA.value,),
        ).fetchall():
            presence = _row(row)
            if machine_fingerprint(presence.get("machine_id")) != fingerprint:
                continue
            if project_id not in set(presence.get("project_ids") or []):
                continue
            if not presence.get("approved_execution_capabilities"):
                continue
            connection.execute(
                """UPDATE agent_presence
                   SET approved_execution_capabilities = '[]', capability_approval_refs = '[]',
                       capability_approved_machine_id = NULL, capability_approved_by = ?,
                       capability_approved_at = ?, updated_at = ?
                   WHERE agent_id = ?""",
                (APPROVAL_REVOKED, timestamp, timestamp, presence["agent_id"]),
            )
            self._event(
                connection, actor, project_id, "qa.capability_approval_revoked",
                "agent_presence", presence["agent_id"], 1,
                {"reason": "qa_machine_infrastructure_failure", "decided_by": APPROVAL_REVOKED},
            )
            revoked.append(presence["agent_id"])
        return revoked

    def _revoke_host_preflight_approvals(
        self, connection: Connection, actor: Actor, project_id: str,
        reason: str, agent_id: str | None = None,
    ) -> list[str]:
        """Withdraw server approvals the changed configuration no longer backs.

        A project's preflight commands, test command or verification profile, or
        an agent's declaration, are what a preflight proved against; after a
        change the agent runs the preflight again. A Planner override is left as
        Planner set it.
        """
        rows = connection.execute(
            "SELECT agent_id, project_ids FROM agent_presence "
            "WHERE role = ? AND capability_approved_by = ?"
            + (" AND agent_id = ?" if agent_id is not None else ""),
            (Role.QA.value, HOST_PREFLIGHT_APPROVER,
             *((agent_id,) if agent_id is not None else ())),
        ).fetchall()
        revoked: list[str] = []
        timestamp = _iso()
        for row in rows:
            project_ids = row["project_ids"]
            if isinstance(project_ids, str):
                project_ids = json.loads(project_ids)
            if project_id not in set(project_ids or []):
                continue
            connection.execute(
                """UPDATE agent_presence
                   SET approved_execution_capabilities = '[]', capability_approval_refs = '[]',
                       capability_approved_by = ?, capability_approved_at = ?, updated_at = ?
                   WHERE agent_id = ?""",
                (APPROVAL_REVOKED, timestamp, timestamp, row["agent_id"]),
            )
            self._event(
                connection, actor, project_id, "qa.capability_approval_revoked",
                "agent_presence", row["agent_id"], 1,
                {"reason": reason, "decided_by": APPROVAL_REVOKED},
            )
            revoked.append(str(row["agent_id"]))
        return revoked

    # --------------------------------------------------------- review jobs

    def _open_capability_review_jobs(
        self, connection: Connection, actor: Actor, project_id: str,
        preflight: dict[str, Any],
    ) -> list[str]:
        """One Planner job per QA-ready ticket blocked on approval alone.

        Called when evidence appears and whenever QA observes the block, so the
        job exists whichever came first. An open job that already cites this
        preflight is left alone: repeating it would only wake the Planner again
        about a decision it already has.
        """
        presence = _row(connection.execute(
            "SELECT * FROM agent_presence WHERE agent_id = ?", (preflight["agent_id"],)
        ).fetchone())
        if presence is None:
            return []
        proven = set(preflight["proven"])
        ref = _PREFLIGHT_REF + preflight["preflight_id"]
        opened: list[str] = []
        for blocker in self._qa_queue_capability_blockers(connection, project_id, presence):
            if blocker["missing_declaration"] or not blocker["missing_approval"]:
                # Undeclared equipment is a registration or provisioning gap,
                # not the approval cycle; it has its own route.
                continue
            ticket = self._ticket(connection, blocker["ticket_id"])
            existing = _row(connection.execute(
                """
                SELECT planner_job_id, evidence_refs FROM planner_jobs
                WHERE ticket_id = ? AND status IN ('READY', 'IN_PROGRESS')
                """,
                (ticket["ticket_id"],),
            ).fetchone())
            if existing is not None and (
                ref in existing["evidence_refs"]
                or not any(item.startswith(_REVIEW_MARK) for item in existing["evidence_refs"])
            ):
                # Already cited, or the open job is a failure decision: a review
                # merged into it would let a capability approval close it (C2).
                # The review opens once that job resolves and QA polls again.
                continue
            missing = sorted(blocker["missing_approval"])
            approvable = sorted(set(missing) & proven)
            unproven = sorted(set(missing) - proven)
            if not unproven:
                owner, action = "planner", "capability_approval"
                summary = (
                    f"QA capability review [owner=planner action=capability_approval]: "
                    f"{ticket['ticket_id']} is QA-ready and blocked only on approval. The host "
                    f"preflight {ref} proves {', '.join(approvable)} for QA {presence['agent_id']}. "
                    f"Call planner.configure_agent_capability_profile with those capabilities "
                    f"and evidence_refs=[\"{ref}\"]; approving closes this job."
                )
            else:
                owner, action = "infrastructure", "capability_preflight_gap"
                reasons = ", ".join(
                    f"{c}: {preflight['outcomes'][c]['reason']}" if c in preflight["outcomes"]
                    else f"{c}: not_preflighted" for c in unproven
                )
                summary = (
                    f"QA capability review [owner=infrastructure action=capability_preflight_gap]: "
                    f"{ticket['ticket_id']} is QA-ready but the host preflight {ref} cannot prove "
                    f"{', '.join(unproven)} ({reasons}). Provision the equipment on the QA "
                    f"machine, or configure a preflight with planner.configure_capability_preflight, "
                    f"then have QA rerun qa.run_capability_preflight."
                )
            candidate = _row(connection.execute(
                "SELECT candidate_id, commit_sha FROM candidates WHERE ticket_id = ? "
                "ORDER BY submitted_at DESC LIMIT 1",
                (ticket["ticket_id"],),
            ).fetchone())
            job = self._open_planner_job(
                connection, actor, project_id, ticket,
                job_type=PlannerJobType.QA_FAILURE,
                summary=summary,
                evidence_refs=[
                    _REVIEW_MARK + presence["agent_id"], ref,
                    f"owner:{owner}", f"action:{action}",
                ],
                candidate_id=candidate["candidate_id"] if candidate else None,
                severity=InterventionSeverity.NORMAL,
                artifact_commit=candidate["commit_sha"] if candidate else None,
            )
            opened.append(job["planner_job_id"])
        return opened

    def _settle_capability_blocks(self, actor: Actor, project_id: str) -> dict[str, Any] | None:
        """From a QA inbox: attach the latest preflight to any approval block.

        Starts no process. With no current preflight the QA agent is told to run
        one; with one, the Planner job is opened if it does not exist yet.
        """
        with self.store.connection(immediate=True) as connection:
            presence = _row(connection.execute(
                "SELECT * FROM agent_presence WHERE agent_id = ?", (actor.agent_id,)
            ).fetchone())
            fingerprint = machine_fingerprint((presence or {}).get("machine_id"))
            if presence is None or fingerprint is None:
                return None
            latest = _row(connection.execute(
                """
                SELECT preflight_id FROM capability_preflights
                WHERE project_id = ? AND agent_id = ? AND machine_fingerprint = ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (project_id, actor.agent_id, fingerprint),
            ).fetchone())
            if latest is None:
                return None
            record = self._preflight(connection, latest["preflight_id"])
            if (
                presence.get("capability_approved_by") == APPROVAL_REVOKED
                and record["created_at"] <= (presence.get("capability_approved_at") or "")
            ):
                # Taken before the configuration changed: the agent reruns it.
                return None
            approval = self._approve_from_preflight(connection, project_id, record)
            jobs = self._open_capability_review_jobs(connection, actor, project_id, record)
        return {
            **self._preflight_summary(record), "approved": approval["approved"],
            "planner_jobs": jobs,
        }

    def latest_capability_preflight_refs(
        self, connection: Connection, project_id: str, agent_id: str,
        machine_id: str | None,
    ) -> tuple[list[str], list[str]]:
        """The newest preflight ref and what it proves, for the Planner's review event."""
        fingerprint = machine_fingerprint(machine_id)
        if fingerprint is None:
            return [], []
        row = _row(connection.execute(
            """
            SELECT preflight_id, outcomes FROM capability_preflights
            WHERE project_id = ? AND agent_id = ? AND machine_fingerprint = ?
            ORDER BY created_at DESC LIMIT 1
            """,
            (project_id, agent_id, fingerprint),
        ).fetchone())
        if row is None:
            return [], []
        outcomes = json.loads(row["outcomes"])
        return (
            [_PREFLIGHT_REF + row["preflight_id"]],
            sorted(c for c, o in outcomes.items() if o["proven"]),
        )

    # ------------------------------------------------------------- helpers

    def _preflight(self, connection: Connection, preflight_id: str) -> dict[str, Any]:
        row = self._one(
            connection, "SELECT * FROM capability_preflights WHERE preflight_id = ?",
            (preflight_id,),
        )
        outcomes = json.loads(row["outcomes"])
        return {
            "preflight_id": row["preflight_id"],
            "project_id": row["project_id"],
            "agent_id": row["agent_id"],
            "machine_fingerprint": row["machine_fingerprint"],
            "host_matches_machine": bool(row["host_matches_machine"]),
            "requested": json.loads(row["requested_capabilities"]),
            "outcomes": outcomes,
            "proven": sorted(c for c, o in outcomes.items() if o["proven"]),
            "created_at": row["created_at"],
        }

    @staticmethod
    def _preflight_summary(record: dict[str, Any]) -> dict[str, Any]:
        """What an agent is shown: outcomes and reasons, never paths or output."""
        return {
            "preflight_ref": _PREFLIGHT_REF + record["preflight_id"],
            "host_matches_machine": record["host_matches_machine"],
            "proven": record["proven"],
            "unproven": {
                capability: outcome["reason"]
                for capability, outcome in record["outcomes"].items()
                if not outcome["proven"]
            },
        }

    @staticmethod
    def _required_execution_capabilities_of(ticket: dict[str, Any]) -> set[str]:
        return {
            str(capability)
            for scenario in ticket.get("validation_contract") or []
            for capability in scenario.get("required_capabilities") or []
        }
