from __future__ import annotations

import hmac
import json
import os
import sqlite3
from pathlib import Path
from typing import Annotated, Any

import anyio

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from hoh_control_plane.domain import (
    Actor,
    AuthenticationError,
    ConflictError,
    DomainError,
    Role,
    require_project,
)
from hoh_control_plane import __version__
from hoh_control_plane.observability import WorkflowMetrics, correlation_scope
from hoh_control_plane.seed import seed_demo as seed_demo_data
from hoh_control_plane.service import ControlPlaneService
from hoh_control_plane.store_factory import store_from_environment


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


# A consequential write may be retried after a lost acknowledgment; the same key
# with the same body replays the original result instead of duplicating state.
IdempotencyKey = Annotated[str | None, Header(alias="Idempotency-Key")]


class ProjectCreate(StrictModel):
    project_id: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9._-]+$")
    name: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=4000)
    default_branch: str = Field(default="main", min_length=1, max_length=200)


class FeatureCreate(StrictModel):
    feature_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9._-]+$")
    title: str = Field(min_length=1, max_length=240)
    objective: str = Field(min_length=1, max_length=8000)
    parent_feature_id: str | None = Field(default=None, max_length=100)
    priority: int = Field(default=50, ge=0, le=100)


class TicketCreate(StrictModel):
    ticket_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9._-]+$")
    title: str = Field(min_length=1, max_length=240)
    objective: str = Field(min_length=1, max_length=8000)
    required_role: Role = Role.DEVELOPER
    acceptance_criteria: list[str] = Field(min_length=1, max_length=30)
    preservation_constraints: list[str] = Field(default_factory=list, max_length=30)
    file_scope: list[str] = Field(default_factory=list, max_length=100)
    dependency_ids: list[str] = Field(default_factory=list, max_length=100)
    ticket_type: str = Field(default="feature_work", min_length=1, max_length=50)
    priority: int = Field(default=50, ge=0, le=100)
    ready: bool = False


class ClaimRequest(StrictModel):
    expected_version: int = Field(ge=1)
    lease_seconds: int = Field(default=900, ge=30, le=3600)


class ProgressRequest(StrictModel):
    lease_id: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1, max_length=8000)


class CandidateRequest(StrictModel):
    lease_id: str = Field(min_length=1, max_length=100)
    expected_version: int = Field(ge=1)
    commit_sha: str = Field(min_length=40, max_length=64, pattern=r"^[0-9a-fA-F]+$")
    base_commit: str = Field(min_length=40, max_length=64, pattern=r"^[0-9a-fA-F]+$")
    branch_ref: str = Field(min_length=1, max_length=300)
    developer_claims: list[str] = Field(default_factory=list, max_length=50)


class EvidenceInput(StrictModel):
    type: str = Field(min_length=1, max_length=50)
    observation: str = Field(min_length=1, max_length=8000)
    storage_ref: str = Field(min_length=1, max_length=1000)


class FindingInput(StrictModel):
    criterion: str = Field(min_length=1, max_length=2000)
    status: str = Field(pattern=r"^(verified|failed|insufficient_evidence|flaky)$")
    severity: str = Field(default="unknown", max_length=30)
    observation: str = Field(min_length=1, max_length=8000)
    evidence: list[EvidenceInput] = Field(min_length=1, max_length=30)


class VerdictRequest(StrictModel):
    lease_id: str = Field(min_length=1, max_length=100)
    candidate_id: str = Field(min_length=1, max_length=100)
    expected_version: int = Field(ge=1)
    verdict: str = Field(pattern=r"^(passed|failed)$")
    findings: list[FindingInput] = Field(min_length=1, max_length=50)


# An idle board does not need a tighter loop than a person can perceive, and a
# heartbeat only has to beat faster than the shortest proxy idle timeout.
_EVENT_STREAM_POLL_SECONDS = 1.0
_EVENT_STREAM_HEARTBEAT_SECONDS = 15.0
_EVENT_STREAM_RETRY_SECONDS = 3.0


def create_app(
    database_path: str | Path | None = None,
    *,
    local_mode: bool | None = None,
    seed_demo: bool = False,
    token_identities: dict[str, Actor] | None = None,
    event_stream_poll_seconds: float = _EVENT_STREAM_POLL_SECONDS,
    event_stream_heartbeat_seconds: float = _EVENT_STREAM_HEARTBEAT_SECONDS,
    event_stream_retry_seconds: float = _EVENT_STREAM_RETRY_SECONDS,
    event_stream_max_idle_polls: int | None = None,
) -> FastAPI:
    db_path = Path(database_path or os.getenv("HOH_DATABASE_PATH", "data/control-plane.db"))
    is_local = (
        local_mode
        if local_mode is not None
        else os.getenv("HOH_LOCAL_MODE", "0").lower() in {"1", "true", "yes"}
    )
    admin_token = os.getenv("HOH_ADMIN_TOKEN")
    identities = token_identities or _token_identities_from_environment()
    store = store_from_environment(db_path)
    service = ControlPlaneService(store)
    if seed_demo:
        seed_demo_data(service)

    app = FastAPI(title="Harness Control Plane", version=__version__)
    app.state.service = service
    app.state.local_mode = is_local
    static_dir = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        # One request is one unit of traced work, and the identifier goes back to
        # the caller so an operator can look up what their own click did.
        with correlation_scope() as correlation_id:
            response = await call_next(request)
        response.headers["X-HOH-Correlation-Id"] = correlation_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; base-uri 'self'; object-src 'none'; "
            "frame-ancestors 'none'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'"
        )
        return response

    @app.exception_handler(DomainError)
    async def domain_error_handler(_request: Request, error: DomainError):
        return JSONResponse(
            status_code=error.status_code,
            content={
                "error": {
                    "code": error.code,
                    "message": str(error),
                    "details": error.details,
                }
            },
        )

    @app.exception_handler(sqlite3.Error)
    async def storage_error_handler(_request: Request, error: sqlite3.Error):
        """Mirror the MCP safety net so raw storage-engine text never reaches a client."""
        return JSONResponse(
            status_code=ConflictError.status_code,
            content={
                "error": {
                    "code": ConflictError.code,
                    "message": (
                        "This change conflicts with the current database state; "
                        "retry the request."
                    ),
                    "details": {},
                }
            },
        )

    @app.exception_handler(Exception)
    async def internal_error_handler(_request: Request, error: Exception):
        """An unexpected failure is reported as an opaque, stable internal error."""
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "internal_error",
                    "message": "The control plane could not complete this request.",
                    "details": {},
                }
            },
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(_request: Request, error: RequestValidationError):
        details = [
            {
                "field": ".".join(str(part) for part in issue["loc"]),
                "message": issue["msg"],
                "code": issue["type"],
            }
            for issue in error.errors()
        ]
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "validation_error",
                    "message": "Request validation failed",
                    "details": details,
                }
            },
        )

    def actor_dependency(
        authorization: Annotated[str | None, Header()] = None,
        x_hoh_agent_id: Annotated[str | None, Header()] = None,
        x_hoh_role: Annotated[str | None, Header()] = None,
    ) -> Actor:
        if is_local:
            if not x_hoh_agent_id or not x_hoh_role:
                raise AuthenticationError("Agent identity headers are required")
            try:
                role = Role(x_hoh_role)
            except ValueError as exc:
                raise AuthenticationError("Unknown agent role") from exc
            return Actor(x_hoh_agent_id, role, frozenset({"*"}))
        if not identities and not admin_token:
            raise AuthenticationError("Server authentication is not configured")
        prefix = "Bearer "
        if not authorization or not authorization.startswith(prefix):
            raise AuthenticationError("Bearer token is required")
        presented_token = authorization[len(prefix) :]
        if not presented_token.isascii():
            raise AuthenticationError("Invalid bearer token")
        if admin_token and hmac.compare_digest(presented_token, admin_token):
            return Actor("api-admin", Role.ADMIN, frozenset({"*"}))
        for token, identity in identities.items():
            if hmac.compare_digest(presented_token, token):
                return identity
        raise AuthenticationError("Invalid bearer token")

    def observer_dependency(request: Request) -> Actor:
        # Native EventSource cannot attach custom local identity headers. Only
        # explicitly local read endpoints get this fallback; writes keep the
        # ordinary identity requirement and production always authenticates.
        if is_local and not request.headers.get("X-HOH-Agent-Id") and not request.headers.get("X-HOH-Role"):
            return Actor("local-observer", Role.PLANNER, frozenset({"*"}))
        return actor_dependency(request.headers.get("Authorization"),
                                request.headers.get("X-HOH-Agent-Id"), request.headers.get("X-HOH-Role"))

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def index() -> HTMLResponse:
        return HTMLResponse((static_dir / "index.html").read_text(encoding="utf-8"))

    @app.get("/api/v1/health")
    def health() -> dict[str, Any]:
        return {"data": {"status": "ok", "version": __version__}}

    @app.get("/api/v1/projects/{project_id}/board")
    def board(project_id: str, actor: Actor = Depends(observer_dependency)) -> dict[str, Any]:
        require_project(actor, project_id)
        return {"data": service.board(project_id)}

    @app.get("/api/v1/projects/{project_id}/dispatch")
    def dispatch_overview(
        project_id: str, actor: Actor = Depends(actor_dependency)
    ) -> dict[str, Any]:
        """Expose durable queue and live-presence state to the read-only dashboard."""
        return {"data": service.dispatch_overview(actor, project_id)}

    @app.post("/api/v1/projects", status_code=201)
    def create_project(
        payload: ProjectCreate,
        actor: Actor = Depends(actor_dependency),
        idempotency_key: IdempotencyKey = None,
    ) -> dict[str, Any]:
        body = payload.model_dump()
        return {
            "data": service.run_idempotent(
                actor,
                operation="planner.create_project",
                idempotency_key=idempotency_key,
                payload=body,
                run=lambda: service.create_project(actor, **body),
            )
        }

    @app.post("/api/v1/projects/{project_id}/features", status_code=201)
    def create_feature(
        project_id: str,
        payload: FeatureCreate,
        actor: Actor = Depends(actor_dependency),
        idempotency_key: IdempotencyKey = None,
    ) -> dict[str, Any]:
        body = payload.model_dump()
        return {
            "data": service.run_idempotent(
                actor,
                operation="planner.create_feature",
                idempotency_key=idempotency_key,
                payload={"project_id": project_id, **body},
                run=lambda: service.create_feature(actor, project_id=project_id, **body),
            )
        }

    @app.post("/api/v1/features/{feature_id}/tickets", status_code=201)
    def create_ticket(
        feature_id: str,
        payload: TicketCreate,
        actor: Actor = Depends(actor_dependency),
        idempotency_key: IdempotencyKey = None,
    ) -> dict[str, Any]:
        body = payload.model_dump()
        return {
            "data": service.run_idempotent(
                actor,
                operation="planner.create_ticket",
                idempotency_key=idempotency_key,
                payload={"feature_id": feature_id, **body},
                run=lambda: service.create_ticket(actor, feature_id=feature_id, **body),
            )
        }

    @app.get("/api/v1/metadata/discover")
    def discover(
        project_id: str,
        dependency_ready: bool = True,
        blocked_only: bool = False,
        limit: int = Query(default=50, ge=1, le=100),
        actor: Actor = Depends(actor_dependency),
    ) -> dict[str, Any]:
        return {
            "data": service.discover_work(
                actor, project_id, dependency_ready, blocked_only, limit
            )
        }

    @app.get("/api/v1/features/{feature_id}/context")
    def feature_context(
        feature_id: str, actor: Actor = Depends(actor_dependency)
    ) -> dict[str, Any]:
        return {"data": service.get_feature_context(actor, feature_id)}

    @app.get("/api/v1/tickets/{ticket_id}/context")
    def ticket_context(
        ticket_id: str, actor: Actor = Depends(actor_dependency)
    ) -> dict[str, Any]:
        return {"data": service.get_ticket_context(actor, ticket_id)}

    @app.post("/api/v1/tickets/{ticket_id}/leases", status_code=201)
    def claim(
        ticket_id: str,
        payload: ClaimRequest,
        actor: Actor = Depends(actor_dependency),
        idempotency_key: IdempotencyKey = None,
    ) -> dict[str, Any]:
        body = payload.model_dump()

        def run() -> dict[str, Any]:
            if actor.role is Role.QA:
                return service.claim_qa(actor, ticket_id, **body)
            return service.claim_ticket(actor, ticket_id, **body)

        return {
            "data": service.run_idempotent(
                actor,
                operation="work.claim",
                idempotency_key=idempotency_key,
                payload={"ticket_id": ticket_id, **body},
                run=run,
            )
        }

    @app.post("/api/v1/tickets/{ticket_id}/progress", status_code=201)
    def progress(
        ticket_id: str,
        payload: ProgressRequest,
        actor: Actor = Depends(actor_dependency),
    ) -> dict[str, Any]:
        return {
            "data": service.append_progress(actor, ticket_id=ticket_id, **payload.model_dump())
        }

    @app.post("/api/v1/tickets/{ticket_id}/candidates", status_code=201)
    def candidate(
        ticket_id: str,
        payload: CandidateRequest,
        actor: Actor = Depends(actor_dependency),
        idempotency_key: IdempotencyKey = None,
    ) -> dict[str, Any]:
        body = payload.model_dump()
        return {
            "data": service.run_idempotent(
                actor,
                operation="dev.submit_candidate",
                idempotency_key=idempotency_key,
                payload={"ticket_id": ticket_id, **body},
                run=lambda: service.submit_candidate(actor, ticket_id=ticket_id, **body),
            )
        }

    @app.post("/api/v1/tickets/{ticket_id}/qa-verdict", status_code=201)
    def qa_verdict(
        ticket_id: str,
        payload: VerdictRequest,
        actor: Actor = Depends(actor_dependency),
        idempotency_key: IdempotencyKey = None,
    ) -> dict[str, Any]:
        body = payload.model_dump()
        return {
            "data": service.run_idempotent(
                actor,
                operation="qa.submit_verdict",
                idempotency_key=idempotency_key,
                payload={"ticket_id": ticket_id, **body},
                run=lambda: service.submit_qa_verdict(actor, ticket_id=ticket_id, **body),
            )
        }

    @app.get("/api/v1/projects/{project_id}/events")
    def events(
        project_id: str,
        after: int = Query(default=0, ge=0),
        actor: Actor = Depends(actor_dependency),
    ) -> dict[str, Any]:
        return {"data": service.list_events(actor, project_id, after)}

    @app.get("/api/v1/projects/{project_id}/events/stream")
    def events_stream(
        request: Request,
        project_id: str,
        after: int = Query(default=0, ge=0),
        last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
        actor: Actor = Depends(observer_dependency),
    ) -> StreamingResponse:
        """Stream bounded change signals so a board can react without polling.

        One-way by construction: this is Server-Sent Events, so a client cannot
        write through it, and every signal still has to be read back through the
        authenticated REST snapshots to learn what actually changed.
        """
        # A reconnecting EventSource resends the last id it saw, which is more
        # trustworthy than a query string the page rebuilt from memory.
        cursor = after
        if last_event_id is not None and last_event_id.isdigit():
            cursor = int(last_event_id)
        # Authorize once, here, so a refused project fails as a normal error
        # response rather than inside an already-open stream.
        service.list_event_signals(actor, project_id, cursor, limit=1)

        async def emit() -> Any:
            position = cursor
            polls_remaining = event_stream_max_idle_polls
            yield f"retry: {int(event_stream_retry_seconds * 1000)}\n\n".encode()
            idle = 0.0
            while True:
                if await request.is_disconnected():
                    return
                batch = await anyio.to_thread.run_sync(service.list_event_signals, actor, project_id, position)
                if batch["reset"]:
                    # The cursor cannot belong to this history, so nothing will
                    # ever arrive after it. Say so instead of hanging.
                    yield b"event: reset\ndata: {}\n\n"
                    position = 0
                    continue
                for signal in batch["events"]:
                    body = json.dumps(signal, sort_keys=True)
                    yield (
                        f"id: {signal['sequence']}\nevent: change\ndata: {body}\n\n"
                    ).encode()
                position = batch["next_cursor"]
                if batch["events"]:
                    idle = 0.0
                    continue
                idle += event_stream_poll_seconds
                if idle >= event_stream_heartbeat_seconds:
                    # Comment frame: keeps an idle connection and any proxy in
                    # front of it from timing the stream out.
                    yield b": heartbeat\n\n"
                    idle = 0.0
                if polls_remaining is not None:
                    if polls_remaining <= 0:
                        return
                    polls_remaining -= 1
                else:
                    await anyio.sleep(event_stream_poll_seconds)

        return StreamingResponse(
            emit(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/v1/projects/{project_id}/metrics")
    def metrics(
        project_id: str,
        actor: Actor = Depends(actor_dependency),
    ) -> dict[str, Any]:
        """Bounded operational measures for one project, aggregates only."""
        return {"data": WorkflowMetrics(store).project_metrics(actor, project_id)}

    @app.get("/api/v1/projects/{project_id}/traces/{correlation_id}")
    def trace(
        project_id: str,
        correlation_id: str,
        actor: Actor = Depends(actor_dependency),
    ) -> dict[str, Any]:
        """The event chain one request produced, without any event payload."""
        return {"data": WorkflowMetrics(store).trace(actor, project_id, correlation_id)}

    @app.get("/api/v1/projects/{project_id}/agent-activity")
    def agent_activity(
        project_id: str,
        after: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=100),
        actor: Actor = Depends(actor_dependency),
    ) -> dict[str, Any]:
        """Return redacted MCP tool activity for the read-only project dashboard."""
        return {"data": service.list_agent_activity(actor, project_id, after, limit)}

    return app


def _token_identities_from_environment() -> dict[str, Actor]:
    raw = os.getenv("HOH_AGENT_TOKENS_JSON")
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return {
            token: Actor(
                value["agent_id"],
                Role(value["role"]),
                frozenset(value.get("project_ids", [])),
            )
            for token, value in parsed.items()
        }
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("HOH_AGENT_TOKENS_JSON is invalid") from exc
