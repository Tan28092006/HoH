"""Re-reading only the part of the plan that moved.

A Planner that re-reads the whole specification every time it wakes spends most
of a context window proving nothing changed. A Planner that does not re-read it
drifts: it plans against the spec it remembers rather than the spec that exists,
and the tickets it writes are subtly for a different system.

Neither is necessary. Hashing the document one section at a time turns "have I
drifted?" into a cursor comparison, and turns "catch up" into reading the two
sections that actually changed. The stored record deliberately holds hashes and
line spans rather than the prose itself: the document stays the source of truth
in git, and this is only the index that says which part of it to open.
"""
from __future__ import annotations

from hoh_control_plane.service._common import (
    Actor,
    Any,
    Connection,
    Role,
    ValidationError,
    _iso,
    _row,
    hashlib,
    re,
    require_project,
    require_role,
)


# An ATX heading, which is the only form this indexes. A setext underline can be
# added when a document in this project actually uses one; guessing at forms
# nothing writes is how a parser acquires untested branches.
_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")

# A fence opens and closes a literal block. Without tracking it, a shell comment
# inside an example becomes a section boundary, and every section after it shifts.
_FENCE = re.compile(r"^\s*(```|~~~)")

_MAX_DOCUMENT_CHARS = 2_000_000
_MAX_DIGEST_SECTIONS = 50


class SpecMixin:
    """Section-level change tracking for the document a project plans against."""

    def sync_spec(
        self, actor: Actor, project_id: str, document: str, text: str
    ) -> dict[str, Any]:
        """Record what each section of the document hashes to now.

        Idempotent by construction: syncing an unchanged document changes no row
        and does not advance the revision, so a Planner polling the digest after
        a no-op sync still sees nothing to read.
        """
        require_role(actor, Role.PLANNER, Role.ADMIN)
        require_project(actor, project_id)
        self._bounded_text(document, "document", 500)
        if not isinstance(text, str):
            raise ValidationError("text must be the document contents")
        if len(text) > _MAX_DOCUMENT_CHARS:
            raise ValidationError("document is too large to index")

        parsed = self._parse_sections(text)
        now = _iso()
        added: list[str] = []
        changed: list[str] = []
        removed: list[str] = []
        unchanged = 0

        with self.store.connection(immediate=True) as connection:
            self._one(
                connection, "SELECT * FROM projects WHERE project_id = ?", (project_id,)
            )
            existing = {
                str(row["heading_path"]): _row(row)
                for row in connection.execute(
                    "SELECT * FROM spec_sections WHERE project_id = ? AND document = ?",
                    (project_id, document),
                ).fetchall()
            }
            revision = self._current_revision(connection, project_id)
            # Worked out before anything is written, so a no-op sync leaves the
            # revision where it was and cannot look like a change to a reader.
            next_revision = revision + 1

            for section in parsed:
                prior = existing.pop(section["heading_path"], None)
                if prior is None:
                    added.append(section["heading_path"])
                elif (
                    prior["content_hash"] != section["content_hash"]
                    or prior["status"] != "present"
                ):
                    changed.append(section["heading_path"])
                else:
                    # Everything derived from the text may be stale even when the
                    # text is not: the lines move when anything above is edited,
                    # and the size and level are recomputed by code that can
                    # itself change. A stale span sends a reader to the wrong
                    # place, so all of it is refreshed while the revision, which
                    # is what says the content moved, stays put.
                    if (
                        prior["start_line"] != section["start_line"]
                        or prior["end_line"] != section["end_line"]
                        or prior["token_estimate"] != section["token_estimate"]
                        or prior["depth"] != section["depth"]
                    ):
                        connection.execute(
                            """
                            UPDATE spec_sections
                            SET start_line = ?, end_line = ?, token_estimate = ?,
                                depth = ?, updated_at = ?
                            WHERE section_id = ?
                            """,
                            (
                                section["start_line"],
                                section["end_line"],
                                section["token_estimate"],
                                section["depth"],
                                now,
                                prior["section_id"],
                            ),
                        )
                    unchanged += 1
                    continue
                self._write_section(
                    connection,
                    project_id=project_id,
                    document=document,
                    section=section,
                    revision=next_revision,
                    now=now,
                    prior=prior,
                )

            for leftover in existing.values():
                if leftover["status"] == "removed":
                    continue
                # A section that disappeared is news: a Planner still planning
                # against it is planning against something that no longer exists.
                removed.append(str(leftover["heading_path"]))
                connection.execute(
                    """
                    UPDATE spec_sections
                    SET status = 'removed', changed_at_revision = ?, updated_at = ?
                    WHERE section_id = ?
                    """,
                    (next_revision, now, leftover["section_id"]),
                )

            touched = bool(added or changed or removed)
            if touched:
                self._set_revision(connection, project_id, next_revision, now)
                self._event(
                    connection,
                    actor,
                    project_id,
                    "spec.synced",
                    "project",
                    project_id,
                    next_revision,
                    {
                        "document": document,
                        "added": len(added),
                        "changed": len(changed),
                        "removed": len(removed),
                    },
                )

        return {
            "document": document,
            "revision": next_revision if touched else revision,
            "added": added,
            "changed": changed,
            "removed": removed,
            "unchanged": unchanged,
        }

    def spec_digest(
        self,
        actor: Actor,
        project_id: str,
        after_revision: int = 0,
        limit: int = _MAX_DIGEST_SECTIONS,
        max_depth: int | None = None,
    ) -> dict[str, Any]:
        """Which sections to open, and nothing else.

        From revision 0 this is the table of contents, which is what a cold agent
        needs. From a later revision it is only what moved since, which is what a
        warm one needs; the common answer is an empty list.

        max_depth trims the outline to its top levels. It matters at the cold
        read: this project's own specification has 106 sections, and listing all
        of them costs half of what the document costs, which would be no saving
        at all. Left off when asking what changed, where precision is the point.
        """
        require_project(actor, project_id)
        if not isinstance(after_revision, int) or isinstance(after_revision, bool):
            raise ValidationError("after_revision must be an integer")
        if after_revision < 0:
            raise ValidationError("after_revision must not be negative")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 200:
            raise ValidationError("limit must be between 1 and 200")
        if max_depth is not None and (
            not isinstance(max_depth, int)
            or isinstance(max_depth, bool)
            or not 1 <= max_depth <= 6
        ):
            raise ValidationError("max_depth must be between 1 and 6")

        with self.store.connection() as connection:
            revision = self._current_revision(connection, project_id)
            if after_revision > revision:
                # A cursor this history cannot have issued: say so rather than
                # answer "nothing changed" forever to an agent that is lost.
                return {
                    "revision": revision,
                    "sections": [],
                    "reset": True,
                    "has_more": False,
                }
            # The depth filter is appended rather than passed as a nullable
            # parameter: PostgreSQL cannot infer the type of an untyped NULL and
            # refuses the statement outright, where SQLite accepts it happily.
            parameters: list[Any] = [project_id, after_revision]
            depth_clause = ""
            if max_depth is not None:
                depth_clause = "AND depth <= ?"
                parameters.append(max_depth)
            parameters.append(limit + 1)
            rows = connection.execute(
                f"""
                SELECT * FROM spec_sections
                WHERE project_id = ? AND changed_at_revision > ?
                  {depth_clause}
                ORDER BY document ASC, start_line ASC
                LIMIT ?
                """,
                tuple(parameters),
            ).fetchall()
            extents = (
                self._subtree_extents(connection, project_id)
                if max_depth is not None
                else {}
            )

        has_more = len(rows) > limit
        sections = []
        for row in rows[:limit]:
            section = _row(row)
            assert section is not None
            # A trimmed outline hides the children, so an entry has to stand for
            # the whole branch: its own span ends at its first child, and sending
            # a reader there would hand them the preamble and call it the section.
            end_line, tokens = extents.get(
                str(section["section_id"]),
                (section["end_line"], section["token_estimate"]),
            )
            sections.append(
                {
                    "document": section["document"],
                    "heading": section["heading_path"],
                    "lines": [section["start_line"], end_line],
                    "tokens": tokens,
                    "status": section["status"],
                }
            )
        return {
            "revision": revision,
            "sections": sections,
            "reset": False,
            "has_more": has_more,
        }

    @staticmethod
    def _subtree_extents(
        connection: Connection, project_id: str
    ) -> dict[str, tuple[int, int]]:
        """How far each section reaches once its nested sections are counted in.

        Computed on read rather than stored: a child's edit would otherwise have
        to write to its parent's row, which is exactly the coupling that makes a
        parent look changed when nothing in it was touched.
        """
        rows = [
            _row(row)
            for row in connection.execute(
                """
                SELECT section_id, document, start_line, end_line, token_estimate, depth
                FROM spec_sections
                WHERE project_id = ? AND status = 'present'
                ORDER BY document ASC, start_line ASC
                """,
                (project_id,),
            ).fetchall()
        ]
        extents: dict[str, tuple[int, int]] = {}
        for index, section in enumerate(rows):
            assert section is not None
            end_line = section["end_line"]
            tokens = section["token_estimate"]
            for following in rows[index + 1 :]:
                assert following is not None
                if following["document"] != section["document"]:
                    break
                if following["depth"] <= section["depth"]:
                    break
                end_line = max(end_line, following["end_line"])
                tokens += following["token_estimate"]
            extents[str(section["section_id"])] = (end_line, tokens)
        return extents

    # --- parsing ----------------------------------------------------------------

    @staticmethod
    def _parse_sections(text: str) -> list[dict[str, Any]]:
        """Split a markdown document into hashed, line-addressed sections."""
        lines = text.splitlines()
        headings: list[tuple[int, int, str]] = []  # line index, level, title
        fence: str | None = None
        for index, line in enumerate(lines):
            fence_match = _FENCE.match(line)
            if fence_match:
                marker = fence_match.group(1)
                if fence is None:
                    fence = marker
                elif fence == marker:
                    fence = None
                continue
            if fence is not None:
                continue
            heading_match = _HEADING.match(line)
            if heading_match:
                headings.append(
                    (index, len(heading_match.group(1)), heading_match.group(2).strip())
                )

        sections: list[dict[str, Any]] = []
        ancestors: list[tuple[int, str]] = []
        seen: dict[str, int] = {}
        for position, (line_index, level, title) in enumerate(headings):
            while ancestors and ancestors[-1][0] >= level:
                ancestors.pop()
            path = " > ".join([title for _, title in ancestors] + [title])
            # Two identical heading paths would collapse into one row and hide a
            # real change, so repeats are numbered rather than merged.
            count = seen.get(path, 0)
            seen[path] = count + 1
            if count:
                path = f"{path} #{count + 1}"
            ancestors.append((level, title))

            end_index = (
                headings[position + 1][0] if position + 1 < len(headings) else len(lines)
            )
            body = "\n".join(lines[line_index:end_index]).strip()
            sections.append(
                {
                    "heading_path": path,
                    "content_hash": hashlib.sha256(body.encode("utf-8")).hexdigest(),
                    "start_line": line_index + 1,
                    "end_line": end_index,
                    # Measured on the prose, not on a JSON rendering of it. The
                    # shared estimator serializes first, and this document is
                    # mostly Vietnamese: every accented character becomes a
                    # six-character escape, which reported sections as costing
                    # half again what reading them actually costs.
                    "token_estimate": len(body) // 4,
                    "depth": level,
                }
            )
        return sections

    # --- storage ----------------------------------------------------------------

    def _write_section(
        self,
        connection: Connection,
        *,
        project_id: str,
        document: str,
        section: dict[str, Any],
        revision: int,
        now: str,
        prior: dict[str, Any] | None,
    ) -> None:
        if prior is not None:
            connection.execute(
                """
                UPDATE spec_sections
                SET content_hash = ?, start_line = ?, end_line = ?, token_estimate = ?,
                    depth = ?, changed_at_revision = ?, status = 'present', updated_at = ?
                WHERE section_id = ?
                """,
                (
                    section["content_hash"],
                    section["start_line"],
                    section["end_line"],
                    section["token_estimate"],
                    section["depth"],
                    revision,
                    now,
                    prior["section_id"],
                ),
            )
            return
        connection.execute(
            """
            INSERT INTO spec_sections
                (section_id, project_id, document, heading_path, content_hash,
                 start_line, end_line, token_estimate, depth, changed_at_revision,
                 status, first_seen_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'present', ?, ?)
            """,
            (
                self._section_id(project_id, document, section["heading_path"]),
                project_id,
                document,
                section["heading_path"],
                section["content_hash"],
                section["start_line"],
                section["end_line"],
                section["token_estimate"],
                section["depth"],
                revision,
                now,
                now,
            ),
        )

    @staticmethod
    def _section_id(project_id: str, document: str, heading_path: str) -> str:
        """Derived, so re-syncing the same section addresses the same row."""
        # repr rather than a joined string: it quotes each part, so no pair of
        # different triples can flatten to the same material and collide.
        material = repr((project_id, document, heading_path))
        return f"spec_{hashlib.sha256(material.encode('utf-8')).hexdigest()[:24]}"

    @staticmethod
    def _current_revision(connection: Connection, project_id: str) -> int:
        row = connection.execute(
            "SELECT revision FROM spec_revisions WHERE project_id = ?", (project_id,)
        ).fetchone()
        return int(row["revision"]) if row else 0

    @staticmethod
    def _set_revision(
        connection: Connection, project_id: str, revision: int, now: str
    ) -> None:
        connection.execute(
            """
            INSERT INTO spec_revisions (project_id, revision, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(project_id) DO UPDATE SET revision = ?, updated_at = ?
            """,
            (project_id, revision, now, revision, now),
        )
