from __future__ import annotations

import hmac
import json
import os
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from hoh_control_plane.domain import (
    Actor,
    AuthenticationError,
    DomainError,
    Role,
)
from hoh_control_plane.seed import seed_demo as seed_demo_data
from hoh_control_plane.service import ControlPlaneService
from hoh_control_plane.store import SQLiteStore


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


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


def create_app(
    database_path: str | Path | None = None,
    *,
    local_mode: bool | None = None,
    seed_demo: bool = False,
    token_identities: dict[str, Actor] | None = None,
) -> FastAPI:
    db_path = Path(database_path or os.getenv("HOH_DATABASE_PATH", "data/control-plane.db"))
    is_local = (
        local_mode
        if local_mode is not None
        else os.getenv("HOH_LOCAL_MODE", "0").lower() in {"1", "true", "yes"}
    )
    admin_token = os.getenv("HOH_ADMIN_TOKEN")
    identities = token_identities or _token_identities_from_environment()
    store = SQLiteStore(db_path)
    store.initialize()
    service = ControlPlaneService(store)
    if seed_demo:
        seed_demo_data(service)

    app = FastAPI(title="Harness Control Plane", version="0.3.0")
    app.state.service = service
    app.state.local_mode = is_local
    static_dir = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
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
        if admin_token and hmac.compare_digest(presented_token, admin_token):
            return Actor("api-admin", Role.ADMIN, frozenset({"*"}))
        for token, identity in identities.items():
            if hmac.compare_digest(presented_token, token):
                return identity
        raise AuthenticationError("Invalid bearer token")

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def index() -> HTMLResponse:
        return HTMLResponse((static_dir / "index.html").read_text(encoding="utf-8"))

    @app.get("/api/v1/health")
    def health() -> dict[str, Any]:
        return {"data": {"status": "ok", "version": "0.3.0"}}

    @app.get("/api/v1/projects/{project_id}/board")
    def board(project_id: str) -> dict[str, Any]:
        return {"data": service.board(project_id)}

    @app.get("/api/v1/projects/{project_id}/dispatch")
    def dispatch_overview(
        project_id: str, actor: Actor = Depends(actor_dependency)
    ) -> dict[str, Any]:
        """Expose durable queue and live-presence state to the read-only dashboard."""
        return {"data": service.dispatch_overview(actor, project_id)}

    @app.post("/api/v1/projects", status_code=201)
    def create_project(
        payload: ProjectCreate, actor: Actor = Depends(actor_dependency)
    ) -> dict[str, Any]:
        return {"data": service.create_project(actor, **payload.model_dump())}

    @app.post("/api/v1/projects/{project_id}/features", status_code=201)
    def create_feature(
        project_id: str,
        payload: FeatureCreate,
        actor: Actor = Depends(actor_dependency),
    ) -> dict[str, Any]:
        return {
            "data": service.create_feature(actor, project_id=project_id, **payload.model_dump())
        }

    @app.post("/api/v1/features/{feature_id}/tickets", status_code=201)
    def create_ticket(
        feature_id: str,
        payload: TicketCreate,
        actor: Actor = Depends(actor_dependency),
    ) -> dict[str, Any]:
        return {
            "data": service.create_ticket(actor, feature_id=feature_id, **payload.model_dump())
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
    ) -> dict[str, Any]:
        if actor.role is Role.QA:
            result = service.claim_qa(actor, ticket_id, **payload.model_dump())
        else:
            result = service.claim_ticket(actor, ticket_id, **payload.model_dump())
        return {"data": result}

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
    ) -> dict[str, Any]:
        return {
            "data": service.submit_candidate(actor, ticket_id=ticket_id, **payload.model_dump())
        }

    @app.post("/api/v1/tickets/{ticket_id}/qa-verdict", status_code=201)
    def qa_verdict(
        ticket_id: str,
        payload: VerdictRequest,
        actor: Actor = Depends(actor_dependency),
    ) -> dict[str, Any]:
        body = payload.model_dump()
        return {
            "data": service.submit_qa_verdict(actor, ticket_id=ticket_id, **body)
        }

    @app.get("/api/v1/projects/{project_id}/events")
    def events(
        project_id: str,
        after: int = Query(default=0, ge=0),
        actor: Actor = Depends(actor_dependency),
    ) -> dict[str, Any]:
        return {"data": service.list_events(actor, project_id, after)}

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
