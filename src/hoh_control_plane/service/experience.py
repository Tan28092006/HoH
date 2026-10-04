"""What working in this code taught, indexed by the code rather than the ticket.

A ticket closes and takes everything learned on it out of reach. The next agent
to touch the same files starts from nothing and pays the same price again: in
this very project one environment trap was written into eight separate ticket
bodies by three different agents before anything surfaced it to the ninth.

So a note is attached to a file scope, not to a ticket, and it is surfaced by
overlap with the scope an agent is about to work in. Nobody has to remember to
go and read it, which is the part that prose in a document never solves.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from hoh_control_plane import scopes
from hoh_control_plane.service._common import (
    Actor,
    Any,
    AuthorizationError,
    Connection,
    Role,
    ValidationError,
    _id,
    _iso,
    _row,
    json,
    require_project,
)


# A packet is a budget, and experience competes with the ticket itself. Three is
# enough for the traps that recur and too few for a place to file everything.
_MAX_EXPERIENCE_NOTES = 3

_MAX_LESSON_CHARS = 2000
_MAX_SURFACED_LESSON_CHARS = 400

# A note not re-seen for this long stops surfacing: the code it describes has
# probably moved on. Recording the same lesson again revives it.
EXPERIENCE_EXPIRY_DAYS = 30
# Ranking weight halves every this many days since a note was last seen, so an
# old, often-seen note cannot crowd out a fresh one forever.
_RECENCY_HALF_LIFE_DAYS = 7.0
# Candidates read from the index before ranking in Python.
_RANKING_POOL = 50


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _expiry_cutoff(now: datetime | None = None) -> str:
    moment = now or datetime.now(timezone.utc)
    return _iso(moment - timedelta(days=EXPERIENCE_EXPIRY_DAYS))


def _rank_score(note: dict[str, Any], now: datetime) -> float:
    age_days = max(0.0, (now - _parse_iso(note["last_seen_at"])).total_seconds() / 86400)
    weight = 1.0 + math.log(max(1, int(note["times_seen"] or 1)))
    return weight * 0.5 ** (age_days / _RECENCY_HALF_LIFE_DAYS)


def _ranked(notes: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    now = datetime.now(timezone.utc)
    notes.sort(key=lambda note: (_rank_score(note, now), note["last_seen_at"]), reverse=True)
    return notes[:limit]


class ExperienceMixin:
    """Recording and surfacing what a scope has already cost someone."""

    def record_experience(
        self,
        actor: Actor,
        ticket_ref: str,
        lesson: str,
        file_scope: list[str] | None = None,
    ) -> dict[str, Any]:
        """File a lesson against a scope, or reinforce one already filed.

        Any role may record: a trap is found by whoever trips over it, and QA
        and the Integrator trip over different ones than a Developer does.
        """
        ticket_id = self._strip_ref(ticket_ref, "ticket")
        self._bounded_text(lesson, "lesson", _MAX_LESSON_CHARS)
        # Normalised once here so the same lesson typed with different spacing
        # reinforces the existing note instead of quietly filing a near-duplicate.
        text = " ".join(lesson.split())
        with self.store.connection(immediate=True) as connection:
            ticket, project_id = self._authorized_ticket(connection, actor, ticket_id)
            scope = self._experience_scope(file_scope, ticket)
            source_commit = self._experience_source_commit(connection, project_id, ticket)
            existing = self._matching_note(connection, project_id, text, scope)
            if existing is not None:
                # The same lesson learned again is not a second lesson. It is
                # evidence that the first one is not reaching people, and the
                # count is what says so.
                connection.execute(
                    """
                    UPDATE experience_notes
                    SET times_seen = times_seen + 1, last_seen_at = ?,
                        source_commit = COALESCE(?, source_commit)
                    WHERE note_id = ?
                    """,
                    (_iso(), source_commit, existing["note_id"]),
                )
                note_id = str(existing["note_id"])
            else:
                note_id = _id(connection, "exp")
                now = _iso()
                connection.execute(
                    """
                    INSERT INTO experience_notes
                        (note_id, project_id, file_scope, lesson, learned_on_ticket,
                         actor_id, actor_role, times_seen, created_at, last_seen_at,
                         source_commit)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                    """,
                    (
                        note_id,
                        project_id,
                        json.dumps(scope),
                        text,
                        ticket_id,
                        actor.agent_id,
                        actor.role.value,
                        now,
                        now,
                        source_commit,
                    ),
                )
                # Indexed as it is written. A note missing from here is a note
                # the surfacing query cannot see, so the two writes belong in
                # the same transaction as each other.
                for root in scopes.roots_of(scope):
                    connection.execute(
                        "INSERT INTO experience_note_scopes (note_id, scope_root) "
                        "VALUES (?, ?)",
                        (note_id, root),
                    )
            self._event(
                connection,
                actor,
                project_id,
                "experience.recorded",
                "ticket",
                ticket_id,
                ticket["version"],
                {"note_id": note_id, "reinforced": existing is not None},
            )
            return self._one(
                connection,
                "SELECT * FROM experience_notes WHERE note_id = ?",
                (note_id,),
            )

    @staticmethod
    def _experience_source_commit(
        connection: Connection, project_id: str, ticket: dict[str, Any]
    ) -> str | None:
        """The ticket's base commit, else the mainline last observed for the project."""
        if ticket.get("base_commit"):
            return str(ticket["base_commit"])
        row = connection.execute(
            "SELECT observed_mainline_commit FROM project_repositories WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        return str(row[0]) if row is not None and row[0] else None

    def _experience_scope(
        self, file_scope: list[str] | None, ticket: dict[str, Any]
    ) -> list[str]:
        """Default to the ticket's own scope, which is where the lesson was paid for."""
        if file_scope is None:
            scope = list(ticket.get("file_scope") or [])
        else:
            if not isinstance(file_scope, list):
                raise ValidationError("file_scope must be a list of path patterns")
            scope = [str(pattern).strip() for pattern in file_scope if str(pattern).strip()]
        if not scope:
            # An empty scope overlaps everything, so it would put this note in
            # front of every agent in the project regardless of relevance.
            raise ValidationError(
                "A lesson needs a file scope to be found by; the ticket has none, "
                "so pass one explicitly",
                details={"reason": "experience_scope_required"},
            )
        return scope

    def _matching_note(
        self,
        connection: Connection,
        project_id: str,
        lesson: str,
        scope: list[str],
    ) -> dict[str, Any] | None:
        """An identical lesson already filed somewhere this scope reaches."""
        rows = connection.execute(
            "SELECT * FROM experience_notes WHERE project_id = ? AND lesson = ? "
            "AND retired_at IS NULL",
            (project_id, lesson),
        ).fetchall()
        for row in rows:
            note = _row(row)
            assert note is not None
            if self._scopes_overlap(scope, note["file_scope"]):
                return note
        return None

    def _relevant_experience(
        self, project_id: str, ticket: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """What the scope this ticket touches has already cost someone else.

        Read-time and by overlap, like inherited constraints: a lesson recorded
        after this ticket started still reaches it. Notes learned on the same
        ticket remain visible because a replacement chat or teammate is not the
        agent that originally paid for the lesson.
        """
        scope = list(ticket.get("file_scope") or [])
        if not scope:
            return []
        clause, parameters = self._overlap_clause(scopes.roots_of(scope))
        with self.store.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM experience_notes
                WHERE project_id = ?
                  AND retired_at IS NULL
                  AND last_seen_at >= ?
                  AND note_id IN (
                      SELECT note_id FROM experience_note_scopes WHERE {clause}
                  )
                ORDER BY last_seen_at DESC
                LIMIT ?
                """,
                (project_id, _expiry_cutoff(), *parameters, _RANKING_POOL),
            ).fetchall()
        notes = [_row(row) for row in rows]
        relevant: list[dict[str, Any]] = []
        for note in _ranked(notes, _MAX_EXPERIENCE_NOTES):
            relevant.append(
                {
                    "note_ref": f"experience:{note['note_id']}",
                    "lesson": self._experience_checkpoint(note["lesson"]),
                    "scope": note["file_scope"],
                    "times_seen": note["times_seen"],
                    "learned_on_ticket": note["learned_on_ticket"],
                    "source_commit": note.get("source_commit"),
                    "last_seen_at": note["last_seen_at"],
                    "recorded_by": note["actor_role"],
                    "advisory": True,
                }
            )
        return relevant

    @staticmethod
    def _experience_checkpoint(lesson: str) -> str:
        if len(lesson) <= _MAX_SURFACED_LESSON_CHARS:
            return lesson
        return lesson[:_MAX_SURFACED_LESSON_CHARS] + "..."

    def get_experience(self, actor: Actor, note_ref: str) -> dict[str, Any]:
        """Resolve one surfaced lesson without loading the project-wide notebook."""
        note_id = self._strip_ref(note_ref, "experience")
        with self.store.connection() as connection:
            note = self._one(
                connection,
                "SELECT * FROM experience_notes WHERE note_id = ?",
                (note_id,),
            )
            require_project(actor, note["project_id"])
        return {
            "note_ref": f"experience:{note_id}",
            "lesson": note["lesson"],
            "scope": note["file_scope"],
            "times_seen": note["times_seen"],
            "learned_on_ticket": note["learned_on_ticket"],
            "source_commit": note.get("source_commit"),
            "last_seen_at": note["last_seen_at"],
            "retired_at": note.get("retired_at"),
            "recorded_by": note["actor_role"],
            "advisory": True,
        }

    def retire_experience(
        self, actor: Actor, note_ref: str, reason: str
    ) -> dict[str, Any]:
        """Stop a note from surfacing. Planner or Admin only; the row is kept."""
        if actor.role not in {Role.PLANNER, Role.ADMIN}:
            raise AuthorizationError("Only Planner or Admin may retire experience")
        note_id = self._strip_ref(note_ref, "experience")
        self._bounded_text(reason, "reason", 2000)
        with self.store.connection(immediate=True) as connection:
            note = self._one(
                connection,
                "SELECT * FROM experience_notes WHERE note_id = ?",
                (note_id,),
            )
            require_project(actor, note["project_id"])
            if note.get("retired_at"):
                return note
            connection.execute(
                "UPDATE experience_notes SET retired_at = ?, retired_by = ?, retired_reason = ? "
                "WHERE note_id = ?",
                (_iso(), actor.agent_id, reason, note_id),
            )
            self._event(
                connection,
                actor,
                note["project_id"],
                "experience.retired",
                "experience",
                note_id,
                0,
                {"note_id": note_id, "reason": reason},
            )
            return self._one(
                connection,
                "SELECT * FROM experience_notes WHERE note_id = ?",
                (note_id,),
            )

    @staticmethod
    def _overlap_clause(roots: list[str]) -> tuple[str, list[Any]]:
        """Ask the index which notes can reach these roots, in two shapes it can use.

        Overlap is "one root contains the other", which splits cleanly: the roots
        that contain this one are a short known list, answerable by equality; the
        roots nested under it are a contiguous range, answerable by comparison.
        Both use an ordinary index on either backend, where a LIKE pattern only
        sometimes does.
        """
        terms: list[str] = []
        parameters: list[Any] = []
        for root in roots:
            ancestors = scopes.ancestors_of(root)
            placeholders = ",".join("?" for _ in ancestors)
            terms.append(f"scope_root IN ({placeholders})")
            parameters.extend(ancestors)
            span = scopes.descendant_range(root)
            if span is not None:
                terms.append("(scope_root >= ? AND scope_root < ?)")
                parameters.extend(span)
        return " OR ".join(terms), parameters

    def list_experience(
        self, actor: Actor, project_id: str, limit: int = _MAX_EXPERIENCE_NOTES
    ) -> list[dict[str, Any]]:
        """The whole file, for a Planner deciding what is worth acting on."""
        require_project(actor, project_id)
        if actor.role not in {Role.PLANNER, Role.ADMIN}:
            raise AuthorizationError(
                "Only Planner or Admin may list project-wide experience"
            )
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise ValidationError("limit must be between 1 and 100")
        with self.store.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM experience_notes
                WHERE project_id = ? AND retired_at IS NULL AND last_seen_at >= ?
                ORDER BY last_seen_at DESC LIMIT ?
                """,
                (project_id, _expiry_cutoff(), max(limit, _RANKING_POOL)),
            ).fetchall()
        return _ranked([_row(row) for row in rows], limit)
