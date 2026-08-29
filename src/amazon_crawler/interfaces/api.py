from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
)
from pydantic import (
    ValidationError as PydanticValidationError,
)
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route

from amazon_crawler.bootstrap import Application, build_application
from amazon_crawler.domain.errors import (
    ConflictError,
    CrawlerError,
    NotFoundError,
    ValidationError,
)
from amazon_crawler.plugins.marketplaces import (
    MARKETPLACES,
    marketplace_default_postal_code,
)


class CreateJobBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    inputs: list[str | dict[str, Any]] = Field(min_length=1, max_length=500)
    kind: str = "amazon.product"
    marketplace_id: str | None = None
    postal_code: str | None = Field(default=None, max_length=32)
    execution_mode: str = "standard"
    priority: int = Field(default=0, ge=-100, le=100)
    max_attempts: int | None = Field(default=None, ge=1, le=20)
    idempotency_key: str | None = Field(default=None, max_length=200)
    options: dict[str, Any] = Field(default_factory=dict)
    confirm_external_write: bool = False


class CookieFillBody(BaseModel):
    """Operational parameters only; runtime credentials are never request fields."""

    model_config = ConfigDict(extra="forbid")

    pool: Literal["default", "overseas"] = "default"
    marketplace_id: str = Field(min_length=2, max_length=8)
    postal_code: str | None = Field(default=None, min_length=1, max_length=32)
    target_count: int = Field(ge=1, le=50)
    confirm_external_write: bool = False

    @field_validator("marketplace_id")
    @classmethod
    def normalize_marketplace(cls, value: str) -> str:
        normalized = value.strip().upper()
        if normalized not in MARKETPLACES:
            raise ValueError("unsupported marketplace")
        return normalized

    @field_validator("postal_code")
    @classmethod
    def normalize_postal_code(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized or any(character in "\r\n\x00" for character in normalized):
            raise ValueError("invalid postal code")
        return normalized


def _error(exc: Exception, status_code: int) -> JSONResponse:
    if isinstance(exc, CrawlerError):
        message = str(exc)
    elif isinstance(exc, (PydanticValidationError, ValueError)):
        # Pydantic messages may echo rejected input values. Requests can
        # contain accidentally supplied credentials, so expose only a fixed
        # validation message at the HTTP boundary.
        message = "request validation failed"
    else:
        message = "internal operation failed"
    return JSONResponse(
        {"ok": False, "error": {"type": exc.__class__.__name__, "message": message}},
        status_code=status_code,
    )


async def _json_body(request: Request) -> dict[str, Any]:
    if (
        request.headers.get("content-length")
        and int(request.headers["content-length"]) > 1_000_000
    ):
        raise ValueError("request body is too large")
    value = await request.json()
    if not isinstance(value, dict):
        raise ValueError("JSON body must be an object")
    return value


def create_app(application: Application | None = None) -> Starlette:
    application = application or build_application()
    static_root = Path(__file__).parent / "static"
    cookie_fill_locks: dict[str, asyncio.Lock] = {}

    @asynccontextmanager
    async def lifespan(_: Starlette):
        worker_task: asyncio.Task[None] | None = None
        delivery_task: asyncio.Task[None] | None = None
        maintenance_task: asyncio.Task[None] | None = None
        if application.settings.worker_enabled:
            worker_task = asyncio.create_task(application.worker.run_forever())
        if application.settings.delivery_worker_enabled:
            delivery_task = asyncio.create_task(
                application.delivery_worker.run_forever()
            )
        if application.cookie_maintenance:
            maintenance_task = asyncio.create_task(
                application.cookie_maintenance.run_forever()
            )
        try:
            yield
        finally:
            if worker_task:
                application.worker.stop()
                await worker_task
            if delivery_task:
                application.delivery_worker.stop()
                await delivery_task
            if maintenance_task and application.cookie_maintenance:
                application.cookie_maintenance.stop()
                await maintenance_task

    async def index(_: Request) -> FileResponse:
        return FileResponse(static_root / "index.html")

    async def health(_: Request) -> JSONResponse:
        cookie, cookie_routes, proxy, upstream = await asyncio.gather(
            application.request_context.cookie_provider.health(),
            application.request_context.cookie_route_health(),
            application.request_context.proxy_provider.health(),
            application.fetcher.health(),
        )
        return JSONResponse(
            {
                "ok": True,
                "status": (
                    "degraded" if upstream["status"] == "degraded" else "ready"
                ),
                "version": "0.2.0",
                "resources": {
                    "cookie": cookie.as_public_dict(),
                    "cookie_routes": cookie_routes,
                    "cookie_harvesters": {
                        pool: {
                            "backend": harvester.backend,
                            "tls_impersonation": harvester.tls_impersonation,
                        }
                        for pool, harvester in application.cookie_harvesters.items()
                    },
                    "proxy": proxy.as_public_dict(),
                    "transport": {
                        "backend": application.fetcher.backend,
                        "tls_impersonation": application.fetcher.tls_impersonation,
                    },
                    "upstream": upstream,
                    "result_storage": {
                        "configured_sinks": application.result_sinks.names,
                        "canonical_sink": "sqlite",
                        "delivery_worker_enabled": application.settings.delivery_worker_enabled,
                    },
                    "legacy_runtime_config": {
                        "loaded": application.settings.legacy_config_loaded,
                        "target_scopes": application.settings.legacy_target_scopes,
                        "values_disclosed": False,
                    },
                },
            }
        )

    async def capabilities(_: Request) -> JSONResponse:
        return JSONResponse({"ok": True, **application.service.capabilities()})

    async def cookie_pools(_: Request) -> JSONResponse:
        pools = []
        for pool_name in ("default", "overseas"):
            harvester = application.cookie_harvesters.get(pool_name)
            pools.append(
                {
                    "id": pool_name,
                    "configured": harvester is not None,
                    "backend": harvester.backend if harvester else None,
                    "tls_impersonation": (
                        harvester.tls_impersonation if harvester else None
                    ),
                }
            )
        return JSONResponse(
            {
                "ok": True,
                "feature": {
                    "api_enabled": application.settings.cookie_operations_api_enabled,
                    "requires_confirmation": True,
                    "accepts_runtime_secrets": False,
                    "max_target_count": 50,
                },
                "pools": pools,
            }
        )

    async def fill_cookie_pool(request: Request) -> JSONResponse:
        body = CookieFillBody.model_validate(await _json_body(request))
        if not application.settings.cookie_operations_api_enabled:
            raise ConflictError(
                "Cookie acquisition API is disabled by deployment policy"
            )
        if not body.confirm_external_write:
            raise ValidationError(
                "Cookie acquisition requires confirmation of external Amazon requests and Redis writes"
            )
        harvester = application.cookie_harvesters.get(body.pool)
        if harvester is None:
            raise ConflictError("selected Cookie pool is not configured")
        postal_code = body.postal_code or marketplace_default_postal_code(
            body.marketplace_id
        )
        if postal_code is None:
            raise ValidationError(
                "selected marketplace has no configured default delivery region"
            )

        lock = cookie_fill_locks.setdefault(body.pool, asyncio.Lock())
        if lock.locked():
            raise ConflictError("Cookie acquisition is already running for this pool")
        async with lock:
            report = await harvester.ensure_capacity(
                body.marketplace_id,
                postal_code,
                body.target_count,
            )
        public_report = asdict(report)
        return JSONResponse(
            {
                "ok": True,
                "pool": body.pool,
                "postal_selection": {
                    "postal_code": postal_code,
                    "source": "explicit" if body.postal_code else "marketplace_default",
                },
                "satisfied": report.available_after >= report.requested,
                "report": public_report,
            }
        )

    async def list_jobs(request: Request) -> JSONResponse:
        limit = int(request.query_params.get("limit", "50"))
        status = request.query_params.get("status")
        jobs = await asyncio.to_thread(
            application.store.list_jobs, limit=limit, status=status
        )
        return JSONResponse({"ok": True, "jobs": jobs})

    async def create_job(request: Request) -> JSONResponse:
        body = CreateJobBody.model_validate(await _json_body(request))
        values = body.model_dump()
        confirmed = bool(values.pop("confirm_external_write"))
        job, created = await asyncio.to_thread(
            application.service.create_job,
            **values,
            external_result_write_authorized=confirmed,
        )
        return JSONResponse(
            {"ok": True, "created": created, "job": job},
            status_code=201 if created else 200,
        )

    async def get_job(request: Request) -> JSONResponse:
        job = await asyncio.to_thread(
            application.store.get_job, request.path_params["job_id"]
        )
        return JSONResponse({"ok": True, "job": job})

    async def results(request: Request) -> JSONResponse:
        values = await asyncio.to_thread(
            application.store.list_results,
            request.path_params["job_id"],
            limit=int(request.query_params.get("limit", "100")),
        )
        return JSONResponse({"ok": True, "results": values})

    async def events(request: Request) -> JSONResponse:
        values = await asyncio.to_thread(
            application.store.list_events,
            request.path_params["job_id"],
            limit=int(request.query_params.get("limit", "100")),
        )
        return JSONResponse({"ok": True, "events": values})

    async def deliveries(request: Request) -> JSONResponse:
        values = await asyncio.to_thread(
            application.store.list_deliveries,
            request.path_params["job_id"],
            limit=int(request.query_params.get("limit", "100")),
        )
        return JSONResponse({"ok": True, "deliveries": values})

    async def control(request: Request) -> JSONResponse:
        action = request.path_params["action"]
        functions = {
            "pause": application.store.request_pause,
            "resume": application.store.resume,
            "cancel": application.store.request_cancel,
        }
        function = functions.get(action)
        if not function:
            return _error(ValueError("unsupported action"), 404)
        job = await asyncio.to_thread(function, request.path_params["job_id"])
        return JSONResponse({"ok": True, "job": job})

    async def metrics(_: Request) -> JSONResponse:
        durable, upstream = await asyncio.gather(
            asyncio.to_thread(application.store.metrics),
            application.fetcher.health(),
        )
        return JSONResponse(
            {
                "ok": True,
                "metrics": durable,
                "upstream": upstream,
                "limits": {
                    "worker_concurrency": application.settings.worker_concurrency,
                    "request_timeout_seconds": (
                        application.settings.request_timeout_seconds
                    ),
                    "max_response_bytes": application.settings.max_response_bytes,
                    "upstream_circuit_failure_threshold": (
                        application.settings.upstream_circuit_failure_threshold
                    ),
                    "upstream_circuit_recovery_seconds": (
                        application.settings.upstream_circuit_recovery_seconds
                    ),
                },
            }
        )

    async def exception_handler(_: Request, exc: Exception) -> JSONResponse:
        if isinstance(exc, NotFoundError):
            return _error(exc, 404)
        if isinstance(exc, ConflictError):
            return _error(exc, 409)
        if isinstance(exc, (CrawlerError, PydanticValidationError, ValueError)):
            return _error(exc, 422)
        return _error(RuntimeError("internal operation failed"), 500)

    routes = [
        Route("/", index),
        Route("/api/v1/health", health),
        Route("/api/v1/capabilities", capabilities),
        Route("/api/v1/cookie-pools", cookie_pools, methods=["GET"]),
        Route("/api/v1/cookie-pools/fill", fill_cookie_pool, methods=["POST"]),
        Route("/api/v1/jobs", list_jobs, methods=["GET"]),
        Route("/api/v1/jobs", create_job, methods=["POST"]),
        Route("/api/v1/jobs/{job_id:str}", get_job, methods=["GET"]),
        Route("/api/v1/jobs/{job_id:str}/results", results, methods=["GET"]),
        Route("/api/v1/jobs/{job_id:str}/events", events, methods=["GET"]),
        Route("/api/v1/jobs/{job_id:str}/deliveries", deliveries, methods=["GET"]),
        Route("/api/v1/jobs/{job_id:str}/{action:str}", control, methods=["POST"]),
        Route("/api/v1/metrics", metrics, methods=["GET"]),
        Route("/favicon.ico", lambda _: FileResponse(static_root / "favicon.svg")),
        Route(
            "/assets/favicon.svg", lambda _: FileResponse(static_root / "favicon.svg")
        ),
        Route("/assets/styles.css", lambda _: FileResponse(static_root / "styles.css")),
        Route("/assets/app.js", lambda _: FileResponse(static_root / "app.js")),
    ]
    return Starlette(
        debug=False,
        routes=routes,
        lifespan=lifespan,
        exception_handlers={
            CrawlerError: exception_handler,
            PydanticValidationError: exception_handler,
            ValueError: exception_handler,
            Exception: exception_handler,
        },
    )
