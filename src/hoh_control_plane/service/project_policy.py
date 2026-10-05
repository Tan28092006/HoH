"""Project policy: test command, verification and capability profiles, validation contracts, skill policy."""
from __future__ import annotations

from hoh_control_plane import contracts
from hoh_control_plane.service._common import (
    _estimate_tokens,
    _CONTRACT_TOKEN_BUDGET,
    _CONTRACT_TEXT_CHARS,
    Actor,
    Any,
    ConflictError,
    Role,
    TicketStatus,
    ValidationError,
    _id,
    _iso,
    _row,
    _transition_ticket,
    json,
    require_project,
    require_role,
)


class ProjectPolicyMixin:
    """Project policy: test command, verification and capability profiles, validation contracts, skill policy."""

    def configure_test_command(
        self,
        actor: Actor,
        project_id: str,
        test_command: list[str],
        timeout_seconds: int,
    ) -> dict[str, Any]:
        """Register the single shell-free test command frozen into future candidates."""
        require_role(actor, Role.PLANNER, Role.ADMIN)
        require_project(actor, project_id)
        self._test_command(test_command)
        if not isinstance(timeout_seconds, int) or isinstance(timeout_seconds, bool):
            raise ValidationError("timeout_seconds must be an integer")
        if not 5 <= timeout_seconds <= 1800:
            raise ValidationError("timeout_seconds must be between 5 and 1800")
        timestamp = _iso()
        with self.store.connection(immediate=True) as connection:
            project = self._one(
                connection, "SELECT * FROM projects WHERE project_id = ?", (project_id,)
            )
            repository = connection.execute(
                "SELECT project_id FROM project_repositories WHERE project_id = ?", (project_id,)
            ).fetchone()
            if repository is None:
                raise ValidationError(
                    "Planner must configure a project Git repository before its test command"
                )
            next_version = project["version"] + 1
            connection.execute(
                """
                INSERT INTO project_test_profiles
                    (project_id, test_command, timeout_seconds, configured_by, configured_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(project_id) DO UPDATE SET
                    test_command = excluded.test_command,
                    timeout_seconds = excluded.timeout_seconds,
                    configured_by = excluded.configured_by,
                    configured_at = excluded.configured_at
                """,
                (project_id, json.dumps(test_command), timeout_seconds, actor.agent_id, timestamp),
            )
            connection.execute(
                "UPDATE projects SET version = ?, updated_at = ? WHERE project_id = ?",
                (next_version, timestamp, project_id),
            )
            self._event(
                connection,
                actor,
                project_id,
                "project.test_command_configured",
                "project",
                project_id,
                next_version,
                {"configured": True, "timeout_seconds": timeout_seconds},
            )
            profile = self._one(
                connection,
                "SELECT * FROM project_test_profiles WHERE project_id = ?",
                (project_id,),
            )
        return profile

    def get_test_profile(self, actor: Actor, project_id: str) -> dict[str, Any]:
        """The project's current test command: what new candidates will freeze."""
        require_role(actor, Role.PLANNER, Role.ADMIN)
        require_project(actor, project_id)
        with self.store.connection() as connection:
            self._one(connection, "SELECT project_id FROM projects WHERE project_id = ?", (project_id,))
            return self._test_profile(connection, project_id)

    @staticmethod
    def _test_profile(connection: Any, project_id: str) -> dict[str, Any]:
        profile = _row(connection.execute(
            "SELECT test_command, timeout_seconds, configured_at, configured_by "
            "FROM project_test_profiles WHERE project_id = ?",
            (project_id,),
        ).fetchone())
        return {
            "project_id": project_id,
            "configured": profile is not None,
            "test_command": profile["test_command"] if profile else None,
            "timeout_seconds": profile["timeout_seconds"] if profile else None,
            "configured_at": profile["configured_at"] if profile else None,
            "configured_by": profile["configured_by"] if profile else None,
        }

    def configure_verification_profile(
        self,
        actor: Actor,
        project_id: str,
        capabilities: list[str],
        quality_policy: str = "claim-evidence-v1",
    ) -> dict[str, Any]:
        """Declare the observable equipment available to QA for this project.

        The profile describes capabilities, not machine-local commands. Commands
        and credentials stay in their dedicated configuration surfaces, while a
        ticket can state which portable capabilities its evidence plan needs.
        """
        require_role(actor, Role.PLANNER, Role.ADMIN)
        require_project(actor, project_id)
        self._bounded_list(capabilities, "capabilities", 50, 100, required=True)
        if quality_policy not in {"legacy", "claim-evidence-v1"}:
            raise ValidationError("quality_policy must be legacy or claim-evidence-v1")
        normalized = sorted(set(capabilities))
        if len(normalized) != len(capabilities):
            raise ValidationError("capabilities cannot contain duplicates")
        for capability in normalized:
            if not capability.replace("_", "").replace("-", "").isalnum():
                raise ValidationError("capabilities must use portable names")
        profile = {
            "schema_version": "1",
            "quality_policy": quality_policy,
            "capabilities": normalized,
        }
        timestamp = _iso()
        with self.store.connection(immediate=True) as connection:
            project = self._one(
                connection, "SELECT * FROM projects WHERE project_id = ?", (project_id,)
            )
            if quality_policy == "claim-evidence-v1":
                proposed_project = {**project, "verification_profile": profile}
                ready_rows = connection.execute(
                    """
                    SELECT t.* FROM tickets t
                    JOIN features f ON f.feature_id = t.feature_id
                    WHERE f.project_id = ? AND t.status = ?
                    """,
                    (project_id, TicketStatus.READY.value),
                ).fetchall()
                for row in ready_rows:
                    self._assert_quality_ready(proposed_project, _row(row) or {})
            next_version = project["version"] + 1
            connection.execute(
                "UPDATE projects SET verification_profile = ?, version = ?, updated_at = ? "
                "WHERE project_id = ?",
                (json.dumps(profile), next_version, timestamp, project_id),
            )
            self._event(
                connection, actor, project_id, "project.verification_profile_configured",
                "project", project_id, next_version,
                {"quality_policy": quality_policy, "capabilities": normalized},
            )
            return self._one(
                connection, "SELECT * FROM projects WHERE project_id = ?", (project_id,)
            )

    def configure_agent_capability_profile(
        self,
        actor: Actor,
        project_id: str,
        agent_id: str,
        capabilities: list[str],
        evidence_refs: list[str],
    ) -> dict[str, Any]:
        """Approve the QA equipment independently verified for one agent.

        Registration remains a session self-report. Dispatch and claim use only
        the intersection of that live declaration and this durable approval, so
        restarting a client cannot grant it new execution authority.
        """
        require_role(actor, Role.PLANNER, Role.ADMIN)
        require_project(actor, project_id)
        self._bounded_text(agent_id, "agent_id", 200)
        self._bounded_list(capabilities, "capabilities", 50, 100)
        self._bounded_list(evidence_refs, "evidence_refs", 20, 1000, required=True)
        normalized = sorted(set(capabilities))
        refs = list(dict.fromkeys(evidence_refs))
        if len(normalized) != len(capabilities):
            raise ValidationError("capabilities cannot contain duplicates")
        if len(refs) != len(evidence_refs):
            raise ValidationError("evidence_refs cannot contain duplicates")
        for capability in normalized:
            if not capability.replace("_", "").replace("-", "").isalnum():
                raise ValidationError("capabilities must use portable names")

        timestamp = _iso()
        with self.store.connection(immediate=True) as connection:
            project = self._one(
                connection, "SELECT * FROM projects WHERE project_id = ?", (project_id,)
            )
            available = set((project.get("verification_profile") or {}).get("capabilities") or [])
            unsupported = sorted(set(normalized) - available)
            if unsupported:
                raise ValidationError(
                    "Agent capability approval exceeds the project's verification profile",
                    {"reason": "verification_capabilities_missing", "capabilities": unsupported},
                )
            presence = self._one(
                connection, "SELECT * FROM agent_presence WHERE agent_id = ?", (agent_id,)
            )
            if presence["role"] != Role.QA.value:
                raise ValidationError("Only QA agents can receive execution capability approval")
            if project_id not in set(presence.get("project_ids") or []):
                raise ValidationError("QA agent is not registered for this project")
            if not presence.get("machine_id"):
                raise ValidationError(
                    "QA agent must register a stable machine_id before capability approval",
                    {"reason": "capability_machine_missing"},
                )
            # The rule the cards used to state and nothing enforced: approve only
            # what this agent declared and what host-run evidence proves on the
            # machine it is registered from.
            undeclared = sorted(set(normalized) - set(presence.get("execution_capabilities") or []))
            if undeclared:
                raise ValidationError(
                    "Approval may cover only capabilities this QA agent has declared",
                    {"reason": "capability_not_declared", "capabilities": undeclared},
                )
            proven = self._proven_by_preflight(connection, project_id, presence, refs)
            unproven = sorted(set(normalized) - proven)
            if unproven:
                raise ValidationError(
                    "Approval exceeds what the cited host preflight proves",
                    {"reason": "capability_not_proven", "capabilities": unproven},
                )
            connection.execute(
                """UPDATE agent_presence
                   SET approved_execution_capabilities = ?, capability_approval_refs = ?,
                       capability_approved_machine_id = ?, capability_approved_by = ?,
                       capability_approved_at = ?, updated_at = ?
                   WHERE agent_id = ?""",
                (
                    json.dumps(normalized), json.dumps(refs), presence["machine_id"],
                    actor.agent_id, timestamp, timestamp, agent_id,
                ),
            )
            self._event(
                connection, actor, project_id, "agent.capability_profile_configured",
                "agent_presence", agent_id, 1,
                {"capabilities": normalized, "evidence_ref_count": len(refs)},
            )
            resolved = self._resolve_capability_review_jobs(
                connection, actor, project_id, agent_id, set(normalized),
                "Capability approved from host preflight " + ", ".join(refs) + ".",
            )
            return {
                **self._one(
                    connection, "SELECT * FROM agent_presence WHERE agent_id = ?", (agent_id,)
                ),
                "resolved_planner_jobs": resolved,
            }

    def _validated_validation_contract(
        self, acceptance_criteria: list[str], contract: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        self._bounded_list(
            contract, "validation_contract", contracts.CONTRACT_MAX_SCENARIOS, 1, required=True, strings_only=False
        )
        allowed_methods = contracts.VALIDATION_METHODS
        normalized: list[dict[str, Any]] = []
        seen: list[str] = []
        for index, item in enumerate(contract):
            if not isinstance(item, dict):
                raise ValidationError("validation_contract entries must be objects")
            allowed = {
                "criterion", "scenario_ref", "method", "expected_observation",
                "required_evidence_types", "required_capabilities",
            }
            unknown = sorted(set(item) - allowed)
            if unknown:
                raise ValidationError(
                    "validation_contract contains unknown fields", {"fields": unknown}
                )
            criterion = str(item.get("criterion", "")).strip()
            scenario_ref = str(item.get("scenario_ref", "")).strip()
            method = str(item.get("method", "")).strip()
            expected = str(item.get("expected_observation", "")).strip()
            evidence_types = item.get("required_evidence_types") or []
            required_capabilities = item.get("required_capabilities") or []
            # The ticket section is protected from compaction, so every character
            # here is carried by every packet for the ticket's whole life.
            self._bounded_text(criterion, f"validation_contract[{index}].criterion", _CONTRACT_TEXT_CHARS)
            self._bounded_text(scenario_ref, f"validation_contract[{index}].scenario_ref", contracts.SCENARIO_REF_CHARS)
            self._bounded_text(expected, f"validation_contract[{index}].expected_observation", _CONTRACT_TEXT_CHARS)
            if method not in allowed_methods:
                raise ValidationError("validation contract method must be black_box, white_box or mixed")
            self._bounded_list(
                evidence_types, f"validation_contract[{index}].required_evidence_types",
                contracts.CONTRACT_MAX_EVIDENCE_TYPES, contracts.EVIDENCE_TYPE_CHARS, required=True,
            )
            self._bounded_list(
                required_capabilities,
                f"validation_contract[{index}].required_capabilities",
                contracts.CONTRACT_MAX_CAPABILITIES, contracts.CAPABILITY_CHARS,
            )
            if len(set(evidence_types)) != len(evidence_types):
                raise ValidationError("required_evidence_types cannot contain duplicates")
            seen.append(criterion)
            normalized.append({
                "criterion": criterion,
                "scenario_ref": scenario_ref,
                "method": method,
                "expected_observation": expected,
                "required_evidence_types": sorted(evidence_types),
                "required_capabilities": sorted(set(required_capabilities)),
            })
        if len(seen) != len(set(seen)) or set(seen) != set(acceptance_criteria):
            raise ValidationError(
                "validation contract must cover every acceptance criterion exactly once",
                {"missing_criteria": sorted(set(acceptance_criteria) - set(seen)),
                 "unknown_criteria": sorted(set(seen) - set(acceptance_criteria))},
            )
        estimated = _estimate_tokens(normalized)
        if estimated > _CONTRACT_TOKEN_BUDGET:
            raise ValidationError(
                f"validation contract is about {estimated} tokens; the ticket section is "
                f"never compacted, so keep the whole contract at or below "
                f"{_CONTRACT_TOKEN_BUDGET} tokens by shortening criteria and observations "
                "or splitting the ticket",
                {"contract_estimated_tokens": estimated, "budget": _CONTRACT_TOKEN_BUDGET},
            )
        return normalized

    def configure_validation_contract(
        self,
        actor: Actor,
        ticket_id: str,
        expected_version: int,
        validation_contract: list[dict[str, Any]],
        reason: str,
    ) -> dict[str, Any]:
        """Bind each acceptance criterion to a reproducible QA observation."""
        require_role(actor, Role.PLANNER, Role.ADMIN)
        self._bounded_text(reason, "reason", 2000)
        with self.store.connection(immediate=True) as connection:
            ticket, project_id = self._authorized_ticket(connection, actor, ticket_id)
            self._require_repository_reconciled(connection, project_id)
            self._expect_version(ticket, expected_version)
            if TicketStatus(ticket["status"]) not in {
                TicketStatus.BACKLOG, TicketStatus.READY, TicketStatus.BLOCKED,
            }:
                raise ConflictError("Validation contract cannot change during active work")
            active_lease = connection.execute(
                "SELECT 1 FROM leases WHERE ticket_id = ? AND expires_at > ?",
                (ticket_id, _iso()),
            ).fetchone()
            if active_lease is not None:
                raise ConflictError("Validation contract cannot change while a lease is active")
            normalized = self._validated_validation_contract(
                ticket["acceptance_criteria"], validation_contract
            )
            if ticket["status"] == TicketStatus.READY.value:
                project = self._one(
                    connection, "SELECT * FROM projects WHERE project_id = ?", (project_id,)
                )
                self._assert_quality_ready(
                    project, {**ticket, "validation_contract": normalized}
                )
            timestamp = _iso()
            next_version = _transition_ticket(
                connection, ticket_id, expected_version=ticket["version"],
                from_statuses=(TicketStatus.BACKLOG, TicketStatus.READY, TicketStatus.BLOCKED),
                changes={"validation_contract": json.dumps(normalized), "updated_at": timestamp},
            )
            entry_id = _id(connection, "ctx")
            connection.execute(
                """
                INSERT INTO context_entries
                    (entry_id, project_id, feature_id, ticket_id, entry_type, content,
                     actor_id, actor_role, created_at)
                VALUES (?, ?, ?, ?, 'validation_contract', ?, ?, ?, ?)
                """,
                (entry_id, project_id, ticket["feature_id"], ticket_id, reason,
                 actor.agent_id, actor.role.value, timestamp),
            )
            self._event(
                connection, actor, project_id, "ticket.validation_contract_configured",
                "ticket", ticket_id, next_version,
                {"scenario_count": len(normalized), "reason": reason},
            )
            return {
                "ticket": self._ticket(connection, ticket_id),
                "context_entry_id": entry_id,
                "contract_estimated_tokens": _estimate_tokens(normalized),
            }

    @staticmethod
    def _assert_quality_ready(project: dict[str, Any], ticket: dict[str, Any]) -> None:
        profile = project.get("verification_profile") or {}
        if profile.get("quality_policy") != "claim-evidence-v1":
            return
        contract = ticket.get("validation_contract") or []
        if not contract:
            raise ValidationError(
                "A validation contract is required before this ticket can be ready",
                {"reason": "validation_contract_missing", "ticket_id": ticket["ticket_id"]},
            )
        available = set(profile.get("capabilities") or [])
        required = {
            capability
            for scenario in contract
            for capability in scenario.get("required_capabilities", [])
        }
        missing = sorted(required - available)
        if missing:
            raise ValidationError(
                "Project verification profile lacks capabilities required by this ticket",
                {"reason": "verification_capabilities_missing", "capabilities": missing},
            )

    def configure_skill_policy(
        self,
        actor: Actor,
        project_id: str,
        accepted_protocol_versions: list[str],
        accepted_skill_versions: dict[str, list[str]],
    ) -> dict[str, Any]:
        """Declare which agent protocol and skill versions a project currently accepts.

        This is mutable policy data, kept in the project's own record rather
        than in any local skill file, so it can be changed without touching or
        redistributing skill assets.
        """
        require_role(actor, Role.PLANNER, Role.ADMIN)
        require_project(actor, project_id)
        self._bounded_list(accepted_protocol_versions, "accepted_protocol_versions", 20, 50)
        if not isinstance(accepted_skill_versions, dict):
            raise ValidationError("accepted_skill_versions must be an object")
        if len(accepted_skill_versions) > 50:
            raise ValidationError("accepted_skill_versions supports at most 50 skills")
        normalized_skill_versions: dict[str, list[str]] = {}
        for skill_name, versions in accepted_skill_versions.items():
            self._bounded_text(skill_name, "accepted_skill_versions key", 100)
            self._bounded_list(
                versions, f"accepted_skill_versions[{skill_name}]", 20, 50, required=True
            )
            normalized_skill_versions[skill_name] = versions
        timestamp = _iso()
        with self.store.connection(immediate=True) as connection:
            project = self._one(
                connection, "SELECT * FROM projects WHERE project_id = ?", (project_id,)
            )
            next_version = project["version"] + 1
            connection.execute(
                """
                INSERT INTO project_skill_policies
                    (project_id, accepted_protocol_versions, accepted_skill_versions,
                     configured_by, configured_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(project_id) DO UPDATE SET
                    accepted_protocol_versions = excluded.accepted_protocol_versions,
                    accepted_skill_versions = excluded.accepted_skill_versions,
                    configured_by = excluded.configured_by,
                    configured_at = excluded.configured_at
                """,
                (
                    project_id,
                    json.dumps(accepted_protocol_versions),
                    json.dumps(normalized_skill_versions),
                    actor.agent_id,
                    timestamp,
                ),
            )
            connection.execute(
                "UPDATE projects SET version = ?, updated_at = ? WHERE project_id = ?",
                (next_version, timestamp, project_id),
            )
            self._event(
                connection,
                actor,
                project_id,
                "project.skill_policy_configured",
                "project",
                project_id,
                next_version,
                {
                    "accepted_protocol_versions": accepted_protocol_versions,
                    "accepted_skill_versions": normalized_skill_versions,
                },
            )
            policy = self._one(
                connection,
                "SELECT * FROM project_skill_policies WHERE project_id = ?",
                (project_id,),
            )
            configured_project = self._one(
                connection, "SELECT * FROM projects WHERE project_id = ?", (project_id,)
            )
        return {"project": configured_project, "policy": policy}

    def get_skill_compatibility(self, actor: Actor, project_id: str) -> dict[str, Any]:
        """Read a project's accepted versions plus each known agent's compatibility.

        Open to every authenticated role with project access: this exposes only
        already-non-secret registration data, not a Planner-only mutation.
        """
        require_project(actor, project_id)
        with self.store.connection() as connection:
            policy = _row(
                connection.execute(
                    "SELECT * FROM project_skill_policies WHERE project_id = ?", (project_id,)
                ).fetchone()
            )
            rows = connection.execute(
                "SELECT * FROM agent_presence WHERE expires_at > ? ORDER BY agent_id",
                (_iso(),),
            ).fetchall()
            agents = []
            for row in rows:
                presence = _row(row)
                if (
                    project_id not in presence["project_ids"]
                    and "*" not in presence["project_ids"]
                ):
                    continue
                agents.append(
                    {
                        "agent_id": presence["agent_id"],
                        "role": presence["role"],
                        "protocol_version": presence["protocol_version"],
                        "skill_manifest": presence["skill_manifest"],
                        "compatibility": presence["skill_compatibility"].get(project_id),
                    }
                )
        return {"project_id": project_id, "policy": policy, "agents": agents}
