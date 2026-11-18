from __future__ import annotations

import json
import logging
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from .auth import JwksKeyResolver, KeyResolver, Principal, authenticate
from .config import Settings
from .errors import ApiError
from .gateway_model import GatewayModel
from .model import FakeModel, ModelPort
from .models import StartRequest, canonical_hash
from .runctx import RunContext
from .runner import Runner
from .store import RunStore, to_run_view
from .tools import ReconcileToolClient

log = logging.getLogger("rero.app")
MAX_BODY_BYTES = 65536


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"code": code, "message": message})


def current_principal(request: Request) -> Principal:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise ApiError(401, "UNAUTHENTICATED", "Missing or invalid service token.")
    state = request.app.state
    return authenticate(
        token.strip(),
        issuer=state.settings.issuer,
        required_audiences=state.settings.required_audiences,
        resolver=state.key_resolver,
    )


def create_app(
    settings: Settings | None = None,
    *,
    key_resolver: KeyResolver | None = None,
    model: ModelPort | None = None,
) -> FastAPI:
    """Builds the app. Settings are validated here: a bad environment stops startup (fail fast)."""
    cfg = settings or Settings()  # raises ValidationError on missing or invalid env
    # httpx logs full request URLs at INFO and the evidence query carries the analyst note.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    store = RunStore(cfg.db_path)
    tools = ReconcileToolClient(cfg.reconcile_internal_base_url, cfg.tool_timeout_seconds)
    gateway: GatewayModel | None = None
    if model is None:
        if cfg.model_adapter == "gateway":
            assert cfg.gateway_base_url
            gateway = GatewayModel(cfg.gateway_base_url, cfg.gateway_timeout_seconds)
            model = gateway
        else:
            model = FakeModel()
    runner = Runner(store, tools, model)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store.init()
        restarted = store.mark_running_failed()
        purged = store.purge_expired()
        log.info("startup restarted=%d purged=%d adapter=%s", restarted, purged, cfg.model_adapter)
        yield
        await runner.shutdown()
        await tools.aclose()
        if gateway is not None:
            await gateway.aclose()

    app = FastAPI(title="RERO", version="0.1.0", lifespan=lifespan)
    app.state.settings = cfg
    app.state.store = store
    app.state.runner = runner
    app.state.key_resolver = key_resolver or JwksKeyResolver(cfg.jwks_uri)

    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        return _error(exc.status, exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, __: RequestValidationError) -> JSONResponse:
        return _error(400, "INVALID_REQUEST", "Request is invalid.")  # never echo submitted values

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        names = {404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED"}
        return _error(exc.status_code, names.get(exc.status_code, "HTTP_ERROR"), "Request cannot be served.")

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "UP"}

    @app.get("/health/ready")
    async def ready() -> JSONResponse:
        ok = store.ping()
        return JSONResponse(status_code=200 if ok else 503, content={"status": "UP" if ok else "DOWN"})

    @app.post("/v1/recommendations")
    async def start(request: Request) -> JSONResponse:
        principal = current_principal(request)
        raw = await request.body()
        if len(raw) > MAX_BODY_BYTES:
            raise ApiError(400, "INVALID_REQUEST", "Request is invalid.")
        try:
            body = StartRequest.model_validate(json.loads(raw))
        except (ValueError, ValidationError) as exc:
            raise ApiError(400, "INVALID_REQUEST", "Request is invalid.") from exc
        key = request.headers.get("idempotency-key")
        if key is None or key != str(body.jobId):
            raise ApiError(400, "INVALID_REQUEST", "Idempotency-Key must equal jobId.")

        row, outcome = store.create_or_get(
            principal.tenant_id,
            key,
            canonical_hash(principal.tenant_id, body),
            str(body.jobId),
            cfg.result_ttl_seconds,
        )
        if outcome == "CONFLICT":
            raise ApiError(409, "IDEMPOTENCY_KEY_REUSED", "Idempotency key was used with different input.")
        if outcome == "CREATED":
            ctx = RunContext(
                run_id=row["run_id"],
                tenant_id=principal.tenant_id,
                job_id=str(body.jobId),
                limits=body.limits,
                note=body.analystNote,
                run_token=body.runToken,
                service_token=principal.token,
            )
            runner.start(ctx)
        return JSONResponse(
            status_code=202 if outcome == "CREATED" else 200,
            content=to_run_view(row),
            headers={"Location": f"/v1/recommendations/{row['run_id']}"},
        )

    @app.get("/v1/recommendations/{run_id}")
    async def get_run(run_id: str, request: Request) -> JSONResponse:
        principal = current_principal(request)
        try:
            uuid.UUID(run_id)
        except ValueError as exc:
            raise ApiError(400, "INVALID_REQUEST", "Request is invalid.") from exc
        row = store.get(principal.tenant_id, run_id)
        if row is None:  # unknown, expired, or another tenant's run: indistinguishable
            raise ApiError(404, "RUN_NOT_FOUND", "Run not found.")
        return JSONResponse(content=to_run_view(row))

    return app
