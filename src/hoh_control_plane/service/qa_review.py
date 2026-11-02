"""Bounded advisory inputs and durable checkpoints, never native task execution."""
from __future__ import annotations

from pydantic import TypeAdapter, ValidationError as SchemaValidationError

from hoh_control_plane import contracts
from hoh_control_plane.service._common import (
    Actor, Any, ConflictError, Role, ValidationError, _id, _iso, json, require_role,
)

_SCOPES = TypeAdapter(contracts.ReviewScopes)
_FOCI = {
    1: ("behavior and contract correctness", "security and authorization", "edge cases and test gaps"),
    2: ("module boundaries and caller compatibility", "state and data-flow invariants", "retries and operational failure recovery"),
}


class QaReviewMixin:
    def _review_batch_binding(self, connection, actor, ticket_id, lease_id):
        require_role(actor, Role.QA)
        ticket, project_id = self._authorized_ticket(connection, actor, ticket_id)
        lease = self._require_active_lease(connection, actor, ticket_id, lease_id)
        candidate = self._current_qa_candidate(connection, ticket)
        self._refuse_self_review(connection, actor, candidate, "review")
        return ticket, project_id, lease, candidate

    @staticmethod
    def _read_review_batch(connection, actor, ticket_id, lease):
        entry = connection.execute(
            "SELECT content FROM context_entries WHERE ticket_id=? AND entry_type='qa_review_batch' "
            "AND actor_id=? AND source_ref=?",
            (ticket_id, actor.agent_id, f"qa-review:{lease['review_id']}"),
        ).fetchone()
        return json.loads(entry["content"]) if entry is not None else None

    def get_review_batch(self, actor: Actor, ticket_id: str, lease_id: str) -> dict[str, Any] | None:
        with self.store.connection() as connection:
            _, _, lease, _ = self._review_batch_binding(connection, actor, ticket_id, lease_id)
            return self._read_review_batch(connection, actor, ticket_id, lease)

    def prepare_review_batch(self, actor: Actor, ticket_id: str, lease_id: str,
                             scopes: list[dict[str, Any]]) -> dict[str, Any]:
        """Freeze three scoped briefs once. Identical retries resume existing tasks."""
        require_role(actor, Role.QA)
        try:
            selected = [scope.model_dump() for scope in _SCOPES.validate_python(scopes)]
        except SchemaValidationError as error:
            raise ValidationError("Review needs exactly three bounded A/B/C scopes") from error
        if {scope["task_key"] for scope in selected} != contracts.REVIEW_TASK_KEYS:
            raise ValidationError("Review scopes must name A, B and C exactly once")
        selected.sort(key=lambda scope: scope["task_key"])
        with self.store.connection(immediate=True) as connection:
            ticket, project_id, lease, candidate = self._review_batch_binding(connection, actor, ticket_id, lease_id)
            changed = {row["path"] for row in connection.execute(
                "SELECT path FROM candidate_changed_paths WHERE candidate_id=?", (candidate["candidate_id"],)
            ).fetchall()}
            tasks = []
            for scope, focus in zip(selected, _FOCI[lease["review_slot"]]):
                paths = scope["paths"] + scope["related_paths"]
                if len(paths) > contracts.REVIEW_MAX_PATHS or len(set(paths)) != len(paths):
                    raise ValidationError("Each leaf needs at most eight distinct files, including related paths")
                for path in paths:
                    if (path.startswith("/") or any(part in {"", ".", ".."} for part in path.split("/"))
                            or any(char in path for char in "\\:*?[]") or any(ord(char) < 32 for char in path)):
                        raise ValidationError("Review paths must be literal repository-relative files")
                if not set(scope["paths"]).issubset(changed):
                    raise ValidationError("Review paths must belong to this frozen candidate's changed paths")
                brief = {
                    **scope, "candidate_id": candidate["candidate_id"], "candidate_commit": candidate["commit_sha"],
                    "candidate_ref": f"{candidate['commit_sha']}^{{commit}}", "focus": focus,
                    "read_only": True, "recursive": False, "run_full_suite": False,
                    "diff_argv": ["git", "diff", "--no-ext-diff", "--no-textconv",
                                  f"{candidate['base_commit']}^{{commit}}", f"{candidate['commit_sha']}^{{commit}}", "--",
                                  *(f":(literal){path}" for path in scope["paths"])],
                    "instructions": "Inspect pinned diffs and listed blobs. Return path/line findings and evidence, or no findings. No edits, claims, heartbeats, verdicts, merges, delegation or full suite. The owner checks mandatory tests.",
                }
                self._bounded_text(json.dumps(brief, ensure_ascii=False), "review brief", contracts.REVIEW_BRIEF_CHARS)
                tasks.append({"task_key": scope["task_key"], "state": "pending", "native_task_id": None,
                              "report_ref": None, "brief": brief})
            previous = self._read_review_batch(connection, actor, ticket_id, lease)
            if previous is not None:
                if [task["brief"] for task in previous["tasks"]] != [task["brief"] for task in tasks]:
                    raise ConflictError("Review scope is frozen; resume the existing batch and native tasks")
                return previous
            batch_id = _id(connection, "ctx")
            batch = {"schema_version": "1", "batch_id": batch_id, "revision": 0,
                     "candidate_id": candidate["candidate_id"], "candidate_commit": candidate["commit_sha"],
                     "review_id": lease["review_id"], "tasks": tasks, "ready_for_synthesis": False}
            connection.execute("""INSERT INTO context_entries
                (entry_id, project_id, feature_id, ticket_id, entry_type, content, source_ref,
                 artifact_commit, actor_id, actor_role, created_at)
                VALUES (?, ?, ?, ?, 'qa_review_batch', ?, ?, ?, ?, ?, ?)""",
                (batch_id, project_id, ticket["feature_id"], ticket_id, json.dumps(batch),
                 f"qa-review:{lease['review_id']}", candidate["commit_sha"], actor.agent_id, actor.role.value, _iso()))
            self._event(connection, actor, project_id, "qa.review_batch_prepared", "ticket", ticket_id,
                        ticket["version"], {"batch_id": batch_id, "review_id": lease["review_id"],
                                            "candidate_id": candidate["candidate_id"]})
            return batch

    def checkpoint_review_task(self, actor: Actor, ticket_id: str, lease_id: str, batch_id: str,
                               expected_revision: int, task_key: str, native_task_id: str,
                               state: str, report_ref: str | None = None) -> dict[str, Any]:
        require_role(actor, Role.QA)
        if (not isinstance(task_key, str) or task_key not in contracts.REVIEW_TASK_KEYS
                or not isinstance(state, str) or state not in contracts.REVIEW_TASK_STATES):
            raise ValidationError("Invalid review task key or state")
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValidationError("expected_revision must be a nonnegative integer")
        self._bounded_text(native_task_id, "native_task_id", contracts.NATIVE_TASK_ID_CHARS)
        if state != "running" and report_ref is None:
            raise ValidationError("Terminal review tasks need the actual report reference")
        if report_ref is not None:
            self._bounded_text(report_ref, "report_ref", contracts.STORAGE_REF_CHARS)
        if state == "running" and report_ref is not None:
            raise ValidationError("A running task does not have a terminal report")
        with self.store.connection(immediate=True) as connection:
            ticket, project_id, lease, _ = self._review_batch_binding(connection, actor, ticket_id, lease_id)
            batch = self._read_review_batch(connection, actor, ticket_id, lease)
            if batch is None or batch["batch_id"] != batch_id:
                raise ConflictError("Batch does not belong to this live candidate review")
            task = next(task for task in batch["tasks"] if task["task_key"] == task_key)
            change = {"native_task_id": native_task_id, "state": state, "report_ref": report_ref}
            if all(task[key] == value for key, value in change.items()):
                return batch
            if expected_revision != batch["revision"]:
                raise ConflictError("Review checkpoint is stale; read qa.get_review_batch and retry this callback",
                                    {"expected": expected_revision, "current": batch["revision"]})
            if task["state"] in {"completed", "failed"}:
                raise ConflictError("A terminal report cannot be replaced or restarted")
            if task["native_task_id"] not in {None, native_task_id}:
                raise ConflictError("Resume the existing native task, do not replace it")
            if any(other["task_key"] != task_key and other["native_task_id"] == native_task_id for other in batch["tasks"]):
                raise ConflictError("Each leaf needs a distinct native task ID")
            task.update(change)
            batch["revision"] += 1
            batch["ready_for_synthesis"] = all(task["state"] == "completed" for task in batch["tasks"])
            connection.execute("UPDATE context_entries SET content=? WHERE entry_id=?", (json.dumps(batch), batch_id))
            self._event(connection, actor, project_id, "qa.review_task_checkpointed", "ticket", ticket_id,
                        ticket["version"], {"batch_id": batch_id, "revision": batch["revision"],
                                            "task_key": task_key, "state": state})
            return batch

    def _require_review_batch_complete(self, connection, actor, ticket_id, lease):
        candidate = self._current_qa_candidate(connection, self._ticket(connection, ticket_id))
        required = (candidate.get("qa_review_plan") or {}).get("advisory_reviewers_per_owner", 0)
        batch = self._read_review_batch(connection, actor, ticket_id, lease)
        if (required and batch is None) or (batch is not None and not batch["ready_for_synthesis"]):
            raise ConflictError("Wait for all required reviewer reports before submitting a verdict",
                                {"reason": "review_batch_incomplete", "required": required})
