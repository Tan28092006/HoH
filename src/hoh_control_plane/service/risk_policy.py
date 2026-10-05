"""Model families, checker/maker distinctness and per-candidate risk tiers (wave 7C3).

Two policies a project owns, both enforced by the server rather than by prompt:

* checker_distinct_from_maker -- a QA claim is refused when the QA session's
  model family equals the family of the Developer that froze the candidate.
  An unknown family on either side is allowed but marked ``family_unverified``.
* risk tiers -- computed from the candidate's changed paths (or, before there
  is a candidate, from the ticket's write scope). ``medium`` needs an
  ``adversarial_review`` evidence item before merge; ``high`` additionally
  waits for ``policy.approve_high_risk`` on that exact candidate.
"""
from __future__ import annotations

from hoh_control_plane import scopes
from hoh_control_plane.service._common import (
    Actor,
    Any,
    AuthorizationError,
    ConflictError,
    ReleasePolicyError,
    Role,
    ValidationError,
    _id,
    _iso,
    _row,
    json,
    re,
    require_project,
    require_role,
    Connection,
)

RISK_TIERS = ("low", "medium", "high")
ADVERSARIAL_REVIEW_EVIDENCE = "adversarial_review"

# Families a launcher is expected to report. Any other portable token is
# accepted and stored as given; "other" never proves distinctness.
KNOWN_MODEL_FAMILIES = ("anthropic", "openai", "google", "other")
_FAMILY_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,39}$")

DEFAULT_HIGH_RISK_PATTERNS = (
    "**/store_schema.py",
    "**/store_migrations.py",
    "**/auth.py",
    "**/*credential*",
    "deploy/**",
    "skills/**",
    "docs/spec/**",
)
DEFAULT_MEDIUM_RISK_PATTERNS = (
    "**/service/_common.py",
    "**/domain.py",
    "**/contracts.py",
    "**/store.py",
    "**/postgres_store.py",
)
DEFAULT_MEDIUM_MAX_FILES = 10


class RiskPolicyMixin:
    """Model-family checks, risk classification and the high-risk sign-off."""

    # --- model families -------------------------------------------------

    @staticmethod
    def _normalized_model_family(model_family: str | None) -> str | None:
        if model_family is None or not str(model_family).strip():
            return None
        family = str(model_family).strip().lower()
        # A launcher may pass "anthropic/claude-opus"; the family is the prefix.
        family = family.split("/", 1)[0]
        if not _FAMILY_RE.match(family):
            raise ValidationError(
                "model_family must be a short portable name such as "
                + ", ".join(KNOWN_MODEL_FAMILIES),
                {"reason": "model_family_invalid"},
            )
        return family

    @staticmethod
    def _stamp_candidate_maker(connection: Connection, candidate_id: str, agent_id: str) -> None:
        """Copy the maker's registered family onto the frozen candidate."""
        presence = _row(connection.execute(
            "SELECT model_family, model FROM agent_presence WHERE agent_id = ?", (agent_id,)
        ).fetchone())
        if presence is None:
            return
        connection.execute(
            "UPDATE candidates SET maker_model_family = ?, maker_model = ? WHERE candidate_id = ?",
            (presence.get("model_family"), presence.get("model"), candidate_id),
        )

    def _maker_family(self, connection: Connection, candidate: dict[str, Any]) -> str | None:
        family = candidate.get("maker_model_family")
        if family:
            return family
        # A candidate frozen before the column, or derived by the server, falls
        # back to its submitter's current registration.
        presence = _row(connection.execute(
            "SELECT model_family FROM agent_presence WHERE agent_id = ?",
            (candidate.get("submitted_by"),),
        ).fetchone())
        return (presence or {}).get("model_family")

    def _maker_checker_family_check(
        self, connection: Connection, actor: Actor, project_id: str, ticket_id: str
    ) -> dict[str, Any]:
        """Refuse a same-family checker; otherwise say how distinctness was established.

        Returns {"status": "distinct" | "family_unverified" | "disabled", ...}.
        """
        policy = self._risk_policy(connection, project_id)
        if not policy["checker_distinct_from_maker"]:
            return {"status": "disabled"}
        candidate = _row(connection.execute(
            "SELECT * FROM candidates WHERE ticket_id = ? ORDER BY submitted_at DESC LIMIT 1",
            (ticket_id,),
        ).fetchone())
        if candidate is None:
            return {"status": "family_unverified", "reason": "no_candidate"}
        checker = (_row(connection.execute(
            "SELECT model_family FROM agent_presence WHERE agent_id = ?", (actor.agent_id,)
        ).fetchone()) or {}).get("model_family")
        maker = self._maker_family(connection, candidate)
        result = {
            "candidate_id": candidate["candidate_id"],
            "maker_model_family": maker,
            "checker_model_family": checker,
        }
        if not checker or not maker or "other" in {checker, maker}:
            return {"status": "family_unverified", **result}
        if checker == maker:
            raise AuthorizationError(
                "Checker model family must differ from the maker's "
                "(project policy checker_distinct_from_maker)",
                {"reason": "checker_same_family_as_maker", **result},
            )
        return {"status": "distinct", **result}

    # --- policy ---------------------------------------------------------

    @staticmethod
    def _risk_policy(connection: Connection, project_id: str) -> dict[str, Any]:
        row = _row(connection.execute(
            "SELECT * FROM project_risk_policies WHERE project_id = ?", (project_id,)
        ).fetchone())
        if row is None:
            return {
                "project_id": project_id,
                "configured": False,
                "checker_distinct_from_maker": True,
                "high_risk_patterns": list(DEFAULT_HIGH_RISK_PATTERNS),
                "medium_risk_patterns": list(DEFAULT_MEDIUM_RISK_PATTERNS),
                "medium_max_files": DEFAULT_MEDIUM_MAX_FILES,
            }
        return {
            "project_id": project_id,
            "configured": True,
            "checker_distinct_from_maker": bool(row["checker_distinct_from_maker"]),
            "high_risk_patterns": json.loads(row["high_risk_patterns"]),
            "medium_risk_patterns": json.loads(row["medium_risk_patterns"]),
            "medium_max_files": int(row["medium_max_files"]),
            "configured_by": row["configured_by"],
            "configured_at": row["configured_at"],
        }

    def get_risk_policy(self, actor: Actor, project_id: str) -> dict[str, Any]:
        require_project(actor, project_id)
        with self.store.connection() as connection:
            return self._risk_policy(connection, project_id)

    def configure_risk_policy(
        self,
        actor: Actor,
        project_id: str,
        checker_distinct_from_maker: bool = True,
        high_risk_patterns: list[str] | None = None,
        medium_risk_patterns: list[str] | None = None,
        medium_max_files: int = DEFAULT_MEDIUM_MAX_FILES,
    ) -> dict[str, Any]:
        """Set the project's maker/checker and risk-tier policy (Planner or Admin)."""
        require_role(actor, Role.PLANNER, Role.ADMIN)
        require_project(actor, project_id)
        high = list(DEFAULT_HIGH_RISK_PATTERNS if high_risk_patterns is None else high_risk_patterns)
        medium = list(
            DEFAULT_MEDIUM_RISK_PATTERNS if medium_risk_patterns is None else medium_risk_patterns
        )
        self._bounded_list(high, "high_risk_patterns", 50, 200)
        self._bounded_list(medium, "medium_risk_patterns", 50, 200)
        if not isinstance(medium_max_files, int) or not 1 <= medium_max_files <= 1000:
            raise ValidationError("medium_max_files must be between 1 and 1000")
        timestamp = _iso()
        with self.store.connection(immediate=True) as connection:
            project = self._one(
                connection, "SELECT * FROM projects WHERE project_id = ?", (project_id,)
            )
            connection.execute(
                """
                INSERT INTO project_risk_policies
                    (project_id, checker_distinct_from_maker, high_risk_patterns,
                     medium_risk_patterns, medium_max_files, configured_by, configured_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (project_id) DO UPDATE SET
                    checker_distinct_from_maker = excluded.checker_distinct_from_maker,
                    high_risk_patterns = excluded.high_risk_patterns,
                    medium_risk_patterns = excluded.medium_risk_patterns,
                    medium_max_files = excluded.medium_max_files,
                    configured_by = excluded.configured_by,
                    configured_at = excluded.configured_at
                """,
                (
                    project_id, 1 if checker_distinct_from_maker else 0,
                    json.dumps(high), json.dumps(medium), medium_max_files,
                    actor.agent_id, timestamp,
                ),
            )
            policy = self._risk_policy(connection, project_id)
            self._event(
                connection, actor, project_id, "project.risk_policy_configured",
                "project", project_id, project["version"],
                {
                    "checker_distinct_from_maker": policy["checker_distinct_from_maker"],
                    "medium_max_files": medium_max_files,
                },
            )
            return policy

    # --- classification -------------------------------------------------

    def _scope_hits_pattern(self, scope: str, pattern: str) -> bool:
        """True when a write scope can reach a path the pattern matches."""
        root = scopes.scope_root(scope).replace("\\", "/").strip("/")
        if self._path_matches_scope(root, pattern):
            return True
        pattern_root = scopes.scope_root(pattern).replace("\\", "/").strip("/")
        # A pattern anchored somewhere ("skills/**") is hit by any scope at or
        # under that root; an unanchored one ("**/x.py") only by a path match.
        return bool(pattern_root) and scopes.roots_overlap(root, pattern_root)

    def _classify_risk(
        self,
        policy: dict[str, Any],
        changed_paths: list[str] | None,
        file_scope: list[str],
    ) -> dict[str, Any]:
        reasons: list[str] = []
        tier = "low"
        if changed_paths:
            for path in changed_paths:
                for pattern in policy["high_risk_patterns"]:
                    if self._path_matches_scope(path, pattern):
                        reasons.append(f"high:{path} ~ {pattern}")
                        break
            if reasons:
                tier = "high"
            else:
                for path in changed_paths:
                    for pattern in policy["medium_risk_patterns"]:
                        if self._path_matches_scope(path, pattern):
                            reasons.append(f"medium:{path} ~ {pattern}")
                            break
                if len(changed_paths) > policy["medium_max_files"]:
                    reasons.append(
                        f"medium:{len(changed_paths)} files > {policy['medium_max_files']}"
                    )
                if reasons:
                    tier = "medium"
            basis = "changed_paths"
        else:
            for scope in file_scope or []:
                for pattern in policy["high_risk_patterns"]:
                    if self._scope_hits_pattern(scope, pattern):
                        reasons.append(f"high:scope {scope} ~ {pattern}")
                        break
            if reasons:
                tier = "high"
            else:
                for scope in file_scope or []:
                    for pattern in policy["medium_risk_patterns"]:
                        if self._scope_hits_pattern(scope, pattern):
                            reasons.append(f"medium:scope {scope} ~ {pattern}")
                            break
                if reasons:
                    tier = "medium"
            basis = "write_scope"
        return {"tier": tier, "basis": basis, "reasons": reasons[:5]}

    def _ticket_risk(
        self, connection: Connection, project_id: str, ticket: dict[str, Any]
    ) -> dict[str, Any]:
        """The tier of the ticket's current candidate, or of its scope before one exists."""
        policy = self._risk_policy(connection, project_id)
        candidate = None
        if ticket.get("candidate_commit"):
            candidate = _row(connection.execute(
                "SELECT candidate_id FROM candidates WHERE ticket_id = ? AND commit_sha = ? "
                "ORDER BY submitted_at DESC LIMIT 1",
                (ticket["ticket_id"], ticket["candidate_commit"]),
            ).fetchone())
        changed = None
        if candidate is not None:
            changed = [
                row["path"] for row in connection.execute(
                    "SELECT path FROM candidate_changed_paths WHERE candidate_id = ? ORDER BY path",
                    (candidate["candidate_id"],),
                ).fetchall()
            ]
        risk = self._classify_risk(policy, changed, ticket.get("file_scope") or [])
        requires = ["suite", "scenarios"]
        if risk["tier"] in {"medium", "high"}:
            requires.append(ADVERSARIAL_REVIEW_EVIDENCE)
        if risk["tier"] == "high":
            requires.append("policy.approve_high_risk")
        risk["requires"] = requires
        if candidate is not None:
            risk["candidate_id"] = candidate["candidate_id"]
            if risk["tier"] == "high":
                risk["high_risk_approved"] = self._high_risk_approval(
                    connection, candidate["candidate_id"]
                ) is not None
        return risk

    @staticmethod
    def _high_risk_approval(connection: Connection, candidate_id: str) -> dict[str, Any] | None:
        return _row(connection.execute(
            "SELECT * FROM high_risk_approvals WHERE candidate_id = ? "
            "ORDER BY approved_at DESC LIMIT 1",
            (candidate_id,),
        ).fetchone())

    def _require_risk_tier_cleared(
        self,
        connection: Connection,
        project_id: str,
        ticket: dict[str, Any],
        candidate: dict[str, Any],
    ) -> dict[str, Any]:
        """Refuse a merge whose candidate lacks what its risk tier requires."""
        risk = self._ticket_risk(connection, project_id, ticket)
        if risk["tier"] == "low":
            return risk
        candidate_id = candidate["candidate_id"]
        review = connection.execute(
            "SELECT evidence_id FROM evidence WHERE candidate_id = ? AND evidence_type = ? LIMIT 1",
            (candidate_id, ADVERSARIAL_REVIEW_EVIDENCE),
        ).fetchone()
        if review is None:
            raise ReleasePolicyError(
                f"A {risk['tier']}-risk candidate needs an {ADVERSARIAL_REVIEW_EVIDENCE} "
                "evidence item before merge",
                {"reason": "adversarial_review_missing", "risk": risk},
            )
        if risk["tier"] == "high" and self._high_risk_approval(connection, candidate_id) is None:
            raise ReleasePolicyError(
                "A high-risk candidate waits for policy.approve_high_risk before merge",
                {"reason": "high_risk_signoff_missing", "risk": risk},
            )
        return risk

    def approve_high_risk(self, actor: Actor, ticket_id: str, reason: str) -> dict[str, Any]:
        """Record the human/Planner policy sign-off for the ticket's current candidate."""
        require_role(actor, Role.PLANNER, Role.ADMIN)
        self._bounded_text(reason, "reason", 600)
        if not reason.strip():
            raise ValidationError("A high-risk sign-off needs a reason")
        with self.store.connection(immediate=True) as connection:
            project_id = self._project_for_ticket(connection, ticket_id)
            require_project(actor, project_id)
            ticket = self._ticket(connection, ticket_id)
            if not ticket.get("candidate_commit"):
                raise ConflictError("Ticket has no frozen candidate to sign off")
            candidate = _row(connection.execute(
                "SELECT * FROM candidates WHERE ticket_id = ? AND commit_sha = ? "
                "ORDER BY submitted_at DESC LIMIT 1",
                (ticket_id, ticket["candidate_commit"]),
            ).fetchone())
            if candidate is None:
                raise ConflictError("Ticket has no frozen candidate to sign off")
            risk = self._ticket_risk(connection, project_id, ticket)
            approval_id = _id(connection, "hrisk")
            timestamp = _iso()
            connection.execute(
                """
                INSERT INTO high_risk_approvals
                    (approval_id, ticket_id, candidate_id, reason, approved_by,
                     approved_role, approved_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    approval_id, ticket_id, candidate["candidate_id"], reason.strip(),
                    actor.agent_id, actor.role.value, timestamp,
                ),
            )
            self._event(
                connection, actor, project_id, "policy.high_risk_approved", "ticket",
                ticket_id, ticket["version"],
                {
                    "approval_id": approval_id,
                    "candidate_id": candidate["candidate_id"],
                    "risk_tier": risk["tier"],
                },
            )
            return {
                "approval_id": approval_id,
                "ticket_id": ticket_id,
                "candidate_id": candidate["candidate_id"],
                "risk": risk,
                "approved_by": actor.agent_id,
                "approved_at": timestamp,
            }
