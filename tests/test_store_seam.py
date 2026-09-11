"""The service layer writes against a seam, not against a driver (HOH-246)."""
from __future__ import annotations

import pathlib
import re

import pytest

from hoh_control_plane.store import (
    SQLITE_DIALECT,
    Dialect,
    StoreConnection,
    StoreIntegrityError,
    translate_placeholders,
)


SERVICE_PACKAGE = pathlib.Path(__file__).resolve().parents[1] / "src/hoh_control_plane/service"


# --- the coupling this ticket exists to remove --------------------------------


def test_no_service_module_names_the_driver_to_do_its_work() -> None:
    """The seam is only real if nothing reaches around it."""
    offenders = {}
    for module in sorted(SERVICE_PACKAGE.glob("*.py")):
        if module.name == "_common.py":
            # The seam's own boundary: this is where the driver is allowed to be
            # named, because naming it exactly once is what the seam is for.
            continue
        source = module.read_text(encoding="utf-8")
        hits = [line.strip() for line in source.splitlines() if "sqlite3" in line]
        if hits:
            offenders[module.name] = hits
    assert offenders == {}


def test_every_statement_is_still_written_in_one_placeholder_style() -> None:
    """A call site must not need to know which backend it is talking to."""
    for module in sorted(SERVICE_PACKAGE.glob("*.py")):
        source = module.read_text(encoding="utf-8")
        assert "%s" not in re.sub(r"#.*", "", source), module.name


# --- placeholder translation --------------------------------------------------


def test_qmark_is_left_alone_for_a_backend_that_wants_it() -> None:
    statement = "SELECT * FROM tickets WHERE ticket_id = ? AND version = ?"

    assert translate_placeholders(statement, "?") == statement


def test_markers_are_rewritten_for_a_backend_that_wants_another_style() -> None:
    statement = "SELECT * FROM tickets WHERE ticket_id = ? AND version = ?"

    translated = translate_placeholders(statement, "%s")

    assert translated == "SELECT * FROM tickets WHERE ticket_id = %s AND version = %s"
    assert "?" not in translated


def test_a_question_mark_inside_a_string_literal_is_data_not_a_marker() -> None:
    """Rewriting it would silently corrupt the statement."""
    statement = "SELECT * FROM t WHERE note = 'is it broken? unclear' AND id = ?"

    translated = translate_placeholders(statement, "%s")

    assert translated == "SELECT * FROM t WHERE note = 'is it broken? unclear' AND id = %s"


def test_an_escaped_quote_does_not_end_the_literal_early() -> None:
    statement = "SELECT * FROM t WHERE note = 'it''s unclear? really' AND id = ?"

    translated = translate_placeholders(statement, "%s")

    assert translated.endswith("AND id = %s")
    assert "'it''s unclear? really'" in translated


def test_a_double_quoted_identifier_is_also_left_intact() -> None:
    statement = 'SELECT "odd?column" FROM t WHERE id = ?'

    translated = translate_placeholders(statement, "%s")

    assert translated == 'SELECT "odd?column" FROM t WHERE id = %s'


# --- what a backend has to declare -------------------------------------------


def test_the_sqlite_dialect_states_what_it_provides_implicitly() -> None:
    """A second backend should be a list of decisions, not a hunt through the schema."""
    assert SQLITE_DIALECT.placeholder == "?"
    assert SQLITE_DIALECT.foreign_keys_statement, "SQLite needs this turned on per connection"
    assert SQLITE_DIALECT.alters_constraints_in_place is False, (
        "this is the whole reason for the rename-create-copy-drop migrations"
    )
    assert "AUTOINCREMENT" in SQLITE_DIALECT.identity_column


def test_a_dialect_names_every_decision_a_backend_must_make() -> None:
    fields = set(Dialect.__dataclass_fields__)

    assert fields == {
        "name",
        "placeholder",
        "identity_column",
        "foreign_keys_statement",
        "lock_timeout_statement",
        "alters_constraints_in_place",
    }


# --- error translation --------------------------------------------------------


def test_a_constraint_violation_arrives_as_a_control_plane_error(store) -> None:
    """Not as driver text a caller would have to parse."""
    with pytest.raises(StoreIntegrityError) as error:
        with store.connection(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO features (feature_id, project_id, title, objective, status,
                                      priority, version, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("orphan", "no-such-project", "t", "o", "planned", 50, 1, "now", "now"),
            )

    assert "sqlite" not in str(error.value).lower()
    assert error.value.code == "state_conflict"


def test_contention_still_surfaces_as_its_own_retryable_error(store) -> None:
    """The seam relocates this behaviour; it does not replace it."""
    from hoh_control_plane.store import ContentionError

    assert issubclass(ContentionError, Exception)
    assert ContentionError.code == "database_contention"
    assert ContentionError.status_code == 503


# --- the seam stays a seam ----------------------------------------------------


def test_the_connection_passes_through_what_it_does_not_shape(store) -> None:
    with store.connection() as connection:
        assert isinstance(connection, StoreConnection)
        # row_factory belongs to the driver and is reached through, not wrapped.
        assert connection.row_factory is not None
        assert connection.raw is not None


def test_reads_still_return_rows_the_service_layer_can_index(store) -> None:
    with store.connection() as connection:
        row = connection.execute("SELECT 1 AS answer").fetchone()

    assert row["answer"] == 1
