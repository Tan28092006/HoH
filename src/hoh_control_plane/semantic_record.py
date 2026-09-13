"""The record a merge leaves in the repository about why a change exists.

The database is authoritative for work in flight. This is what survives into the
repository once work has settled, so a clone carries not only the code but the
ticket it served, what it had to satisfy, and the evidence that showed it did.

Only settled facts are written here, and only ones a reader in another clone can
act on. Anything machine-local — a worktree path, a lease, a credential — is
meaningless elsewhere and is excluded, not redacted after the fact.
"""
from __future__ import annotations

from typing import Any


RECORD_DIRECTORY = "docs/decisions"

# Facts that belong to this machine or this moment, never to the record. Listed
# rather than filtered by guesswork, so adding a field to a ticket cannot quietly
# start leaking one of these into the repository.
EXCLUDED_FIELDS = frozenset(
    {
        "lease_id",
        "worktree_path",
        "repository_path",
        "report_path",
        "branch_ref",
        "agent_id",
        "requested_by",
        "submitted_by",
        "executed_by",
        "created_by",
    }
)


def record_path(ticket_id: str) -> str:
    """Where one ticket's record lives. One file per ticket, not per table."""
    safe = "".join(
        character for character in ticket_id if character.isalnum() or character in "._-"
    )
    # A run of dots is what a traversal attempt leaves behind once the separators
    # are gone, and a leading dot hides the file. Neither belongs in a name that
    # is supposed to be a ticket identifier.
    while ".." in safe:
        safe = safe.replace("..", ".")
    safe = safe.strip("._-")
    if not safe:
        raise ValueError("ticket_id has no characters usable in a path")
    return f"{RECORD_DIRECTORY}/{safe}.md"


def _bullets(values: list[str] | None, empty: str) -> str:
    if not values:
        return f"_{empty}_\n"
    return "".join(f"- {str(value).strip()}\n" for value in values)


def render(
    *,
    ticket: dict[str, Any],
    candidate: dict[str, Any],
    findings: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
    mainline_commit: str,
    supersedes: str | None = None,
) -> str:
    """Render one ticket's settled record as prose a person can read in a diff.

    Prose rather than serialized rows: this is read by people reviewing why a
    change exists, and a diff of it should show a changed decision as a changed
    sentence rather than as a moved field.
    """
    lines: list[str] = []
    lines.append(f"# {ticket['ticket_id']} — {ticket.get('title', '').strip()}\n")
    if supersedes:
        # A correction never edits what it corrects; it says what it replaces.
        lines.append(f"> Supersedes the record at candidate `{supersedes}`.\n")
    lines.append("\n## Why this change exists\n\n")
    lines.append(f"{str(ticket.get('objective', '')).strip()}\n")

    lines.append("\n## What it had to satisfy\n\n")
    lines.append(_bullets(ticket.get("acceptance_criteria"), "No acceptance criteria recorded."))

    contract = ticket.get("validation_contract") or []
    if contract:
        lines.append("\n## How it was verified\n\n")
        for scenario in contract:
            evidence_types = ", ".join(scenario.get("required_evidence_types") or [])
            lines.append(
                f"- `{scenario.get('scenario_ref', '')}` ({scenario.get('method', '')}): "
                f"{scenario.get('expected_observation', '')}; evidence: {evidence_types}\n"
            )

    constraints = ticket.get("preservation_constraints")
    if constraints:
        lines.append("\n## What it had to leave working\n\n")
        lines.append(_bullets(constraints, ""))

    lines.append("\n## What landed\n\n")
    lines.append(f"- Candidate `{candidate.get('commit_sha', '')}`\n")
    lines.append(f"- Based on `{candidate.get('base_commit', '')}`\n")
    lines.append(f"- Merged as `{mainline_commit}`\n")

    lines.append("\n## What independent QA found\n\n")
    if not findings:
        lines.append("_No findings were recorded against the merged candidate._\n")
    else:
        for finding in findings:
            criterion = str(finding.get("criterion", "")).strip()
            status = str(finding.get("status", "")).strip()
            lines.append(f"### {criterion or 'Unnamed criterion'}\n\n")
            lines.append(f"- Verdict: **{status or 'unrecorded'}**\n")
            observation = str(finding.get("observation", "")).strip()
            if observation:
                lines.append(f"- Observed: {observation}\n")
            for reference in finding.get("evidence_refs") or []:
                # The reference, never the payload: evidence can be large and can
                # hold anything a QA agent observed.
                lines.append(f"- Evidence: `{reference}`\n")
            lines.append("\n")

    if decisions:
        lines.append("\n## Decisions that shaped it\n\n")
        for decision in decisions:
            content = str(decision.get("content", "")).strip()
            if content:
                lines.append(f"- {content}\n")

    return "".join(lines)


def assert_carries_nothing_local(rendered: str, forbidden: list[str]) -> None:
    """Refuse to write a record that carries something only this machine knows.

    Checked against the actual values rather than trusted from the renderer, so a
    future field added upstream cannot quietly start leaking one of them.
    """
    for value in forbidden:
        if value and str(value) in rendered:
            raise ValueError(
                "The semantic record would carry a value that is local to this "
                "machine or this moment, which no other clone can act on"
            )
