from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import secrets
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Annotated, Any, Literal

from mcp.server import MCPServer
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.exceptions import MCPError
from mcp.types import ToolAnnotations
from mcp_types.version import KNOWN_PROTOCOL_VERSIONS, LATEST_PROTOCOL_VERSION
from pydantic import Field
from starlette.requests import Request
from starlette.responses import JSONResponse

from amazon_crawler.application.preflight import configuration_report
from amazon_crawler.application.scoped_runner import run_scoped_job
from amazon_crawler.application.service import LOCAL_RESULT_SINKS
from amazon_crawler.bootstrap import Application, build_application
from amazon_crawler.config import Settings
from amazon_crawler.domain.errors import CrawlerError
from amazon_crawler.interfaces.mcp_policy import (
    ApprovalAuthority,
    MCPExecutionPolicy,
    MCPPrincipal,
    MCPRuntimeConfig,
    OAuthJWTTokenVerifier,
    TestSHA256TokenVerifier,
)

SERVER_INSTRUCTIONS = (
    "Call crawler_doctor before creating work. In production, prefer "
    "crawler_create_job and poll crawler_get_job; crawler_run_job may be disabled. "
    "Never pass Cookie, proxy, database, bearer-token, or signing-key secrets as tool "
    "arguments. created=true proves persistence only, not crawl completion. External "
    "result writes and production cancellation require an operator-issued approval "
    "receipt bound to the exact action."
)

ReadOnly = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
)
CreatesWork = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True
)
ControlsWork = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False
)
CancelsWork = ToolAnnotations(
    readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False
)

TaskInput = str | dict[str, Any]
TaskInputs = Annotated[
    list[TaskInput],
    Field(min_length=1, max_length=500, description="Authorized crawl inputs."),
]
TaskStatus = Literal[
    "pending",
    "running",
    "pause_requested",
    "paused",
    "cancel_requested",
    "cancelled",
    "succeeded",
    "partial",
    "failed",
]


def _settings(db_override: Path | None) -> Settings:
    settings = Settings.from_env()
    if db_override is None:
        return settings
    return replace(settings, db_path=db_override.expanduser().resolve())


def _task_options(
    result_sinks: list[str] | None,
    tags: list[str] | None,
    requested_fields: list[str] | None,
) -> dict[str, Any] | None:
    options: dict[str, Any] = {}
    if result_sinks is not None:
        options["result_sinks"] = result_sinks
    if tags is not None:
        options["tags"] = tags
    if requested_fields is not None:
        options["requested_fields"] = requested_fields
    return options or None


def _job_arguments(
    *,
    inputs: list[TaskInput],
    kind: str,
    marketplace_id: str | None,
    postal_code: str | None,
    execution_mode: str,
    priority: int,
    max_attempts: int | None,
    idempotency_key: str | None,
    result_sinks: list[str] | None,
    tags: list[str] | None,
    requested_fields: list[str] | None,
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "inputs": inputs,
        "kind": kind,
        "marketplace_id": marketplace_id,
        "postal_code": postal_code,
        "execution_mode": execution_mode,
        "priority": priority,
        "max_attempts": max_attempts,
        "idempotency_key": idempotency_key,
        "result_sinks": result_sinks,
        "tags": tags,
        "requested_fields": requested_fields,
    }
    if timeout_seconds is not None:
        value["timeout_seconds"] = timeout_seconds
    return value


def _require_runtime_configuration(application: Application) -> None:
    report = configuration_report(application.settings)
    if report["configuration_ready"]:
        return
    raise ToolError(
        json.dumps(
            {
                "error": "runtime configuration is not ready",
                "blocking_issues": report["blocking_issues"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


def _raise_public_tool_error(exc: Exception) -> None:
    if isinstance(exc, CrawlerError):
        raise ToolError(str(exc)) from exc
    raise ToolError("internal crawler operation failed") from exc


def _cursor_offset(cursor: str | None) -> int:
    if cursor is None:
        return 0
    if not cursor.isdigit() or len(cursor) > 12:
        raise ToolError("cursor must be a non-negative decimal offset")
    return int(cursor)


def _compact_item(item: Mapping[str, Any], max_bytes: int) -> dict[str, Any]:
    encoded = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str).encode(
        "utf-8"
    )
    if len(encoded) <= max_bytes:
        return dict(item)
    return {
        "id": item.get("id"),
        "schema_version": item.get("schema_version"),
        "omitted": True,
        "reason": "item_exceeds_mcp_output_limit",
        "byte_count": len(encoded),
        "sha256": hashlib.sha256(encoded).hexdigest(),
    }


def _bounded_page(
    values: list[dict[str, Any]],
    *,
    key: str,
    offset: int,
    requested_limit: int,
    max_bytes: int,
) -> dict[str, Any]:
    has_more = len(values) > requested_limit
    candidates = values[:requested_limit]
    emitted: list[dict[str, Any]] = []
    budget = max(1_000, max_bytes - 1_000)
    used = 0
    for value in candidates:
        compacted = _compact_item(value, max(1_000, budget))
        size = len(
            json.dumps(compacted, ensure_ascii=False, default=str).encode("utf-8")
        )
        if emitted and used + size > budget:
            has_more = True
            break
        emitted.append(compacted)
        used += size
    consumed = len(emitted)
    return {
        "ok": True,
        key: emitted,
        "page": {
            "offset": offset,
            "returned": consumed,
            "requested_limit": requested_limit,
            "next_cursor": str(offset + consumed) if has_more and consumed else None,
            "truncated_by_output_limit": consumed < len(candidates),
        },
    }


def _bounded_job(job: dict[str, Any], max_bytes: int) -> dict[str, Any]:
    if (
        len(json.dumps(job, ensure_ascii=False, default=str).encode("utf-8"))
        <= max_bytes
    ):
        return job
    value = dict(job)
    items = value.get("items") if isinstance(value.get("items"), list) else []
    value["items"] = [
        _compact_item(item, max(1_000, max_bytes // 10))
        for item in items[:25]
        if isinstance(item, Mapping)
    ]
    value["items_truncated"] = len(items) > len(value["items"])
    value["full_item_state_available_via"] = "crawler_get_events"
    if (
        len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"))
        > max_bytes
    ):
        value["items"] = []
        value["items_truncated"] = bool(items)
    return value


def _bounded_run_report(report: dict[str, Any], max_bytes: int) -> dict[str, Any]:
    if (
        len(json.dumps(report, ensure_ascii=False, default=str).encode("utf-8"))
        <= max_bytes
    ):
        return report
    value = dict(report)
    results = value.get("results") if isinstance(value.get("results"), list) else []
    deliveries = (
        value.get("deliveries") if isinstance(value.get("deliveries"), list) else []
    )
    value["results"] = [
        _compact_item(item, max(1_000, max_bytes // 4))
        for item in results[:10]
        if isinstance(item, Mapping)
    ]
    value["deliveries"] = deliveries[:20]
    value["output_truncated"] = True
    value["full_results_available_via"] = "crawler_get_results"
    if (
        len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"))
        <= max_bytes
    ):
        return value
    value["results"] = []
    value["deliveries"] = []
    value["jobs"] = [
        {"id": item.get("id"), "status": item.get("status")}
        for item in value.get("jobs", [])
        if isinstance(item, Mapping)
    ]
    return value


def create_mcp_server(
    application: Application | None = None,
    *,
    settings: Settings | None = None,
    runtime_config: MCPRuntimeConfig | None = None,
) -> MCPServer:
    """Create an authenticated, tenant-scoped MCP adapter."""

    app = application or build_application(settings)
    runtime = runtime_config or MCPRuntimeConfig.from_env()
    policy = MCPExecutionPolicy(runtime, app.store)
    approvals = ApprovalAuthority(runtime, app.store)
    auth = None
    verifier = None
    if runtime.auth_mode in {"oauth", "test-token"}:
        auth = AuthSettings(
            issuer_url=runtime.issuer_url,
            resource_server_url=runtime.resource_server_url,
            required_scopes=None,
        )
        verifier = (
            OAuthJWTTokenVerifier(runtime)
            if runtime.auth_mode == "oauth"
            else TestSHA256TokenVerifier(runtime.test_token_records)
        )
    server = MCPServer(
        name="amazon-crawler",
        title="Amazon Crawler",
        description="Resumable Amazon crawl jobs, status, evidence, and results.",
        instructions=SERVER_INSTRUCTIONS,
        version="0.2.0",
        auth=auth,
        token_verifier=verifier,
        middleware=[policy],
    )

    def principal(tool_name: str) -> MCPPrincipal:
        current = policy.principal()
        policy.require_scope(current, tool_name)
        return current

    def external_write_requested(result_sinks: list[str] | None) -> bool:
        return bool(set(result_sinks or ()) - LOCAL_RESULT_SINKS)

    def verify_approval(
        receipt: str | None,
        *,
        current: MCPPrincipal,
        action: str,
        arguments: Mapping[str, Any],
    ) -> None:
        try:
            approvals.verify_and_consume(
                receipt,
                principal=current,
                action=action,
                arguments=arguments,
            )
        except (CrawlerError, MCPError) as exc:
            raise ToolError(str(exc)) from exc

    async def mcp_operational_report() -> dict[str, Any]:
        try:
            database_ready = await asyncio.to_thread(app.store.healthcheck)
        except Exception:
            database_ready = False
        runtime_metrics = await policy.observability.snapshot(None)
        alerts = await policy.observability.evaluate_alerts(
            tenant_id=None,
            runtime_metrics=runtime_metrics,
            database_ready=database_ready,
        )
        status = (
            "unavailable" if not database_ready else "degraded" if alerts else "ready"
        )
        return {
            "schema": "amazon_crawler_mcp_observability_v1",
            "ok": database_ready,
            "status": status,
            "components": {
                "state_and_audit_store": ("ready" if database_ready else "unavailable"),
            },
            "runtime_metrics": runtime_metrics,
            "alerts": alerts,
            "limits": {
                "rate_limit_per_minute": runtime.rate_limit_per_minute,
                "rate_limit_burst": runtime.rate_limit_burst,
                "max_concurrent_runs": runtime.max_concurrent_runs,
                "run_queue_timeout_seconds": runtime.run_queue_timeout_seconds,
                "max_page_size": runtime.max_page_size,
                "max_tool_output_bytes": runtime.max_tool_output_bytes,
            },
            "server": {
                "latest_protocol_version": LATEST_PROTOCOL_VERSION,
                "authentication": runtime.auth_mode,
                "production": runtime.production,
            },
        }

    def require_internal_observability(request: Request) -> JSONResponse | None:
        if not runtime.internal_observability_enabled:
            return JSONResponse({"error": "not_found"}, status_code=404)
        scheme, separator, token = request.headers.get("authorization", "").partition(
            " "
        )
        candidate = hashlib.sha256(token.encode("utf-8")).hexdigest()
        expected = runtime.internal_observability_token_sha256 or ""
        if (
            not separator
            or scheme.lower() != "bearer"
            or not token
            or not secrets.compare_digest(candidate, expected)
        ):
            return JSONResponse(
                {"error": "unauthorized"},
                status_code=401,
                headers={
                    "Cache-Control": "no-store",
                    "WWW-Authenticate": "Bearer",
                },
            )
        return None

    @server.custom_route("/health/live", methods=["GET"], include_in_schema=False)
    async def health_live(_: Request) -> JSONResponse:
        return JSONResponse({"status": "live"})

    @server.custom_route("/health/ready", methods=["GET"], include_in_schema=False)
    async def health_ready(_: Request) -> JSONResponse:
        report = await mcp_operational_report()
        return JSONResponse(
            {"status": report["status"]},
            status_code=200 if report["ok"] else 503,
        )

    @server.custom_route(
        "/internal/mcp/observability", methods=["GET"], include_in_schema=False
    )
    async def internal_mcp_observability(request: Request) -> JSONResponse:
        denied = require_internal_observability(request)
        if denied is not None:
            return denied
        return JSONResponse(
            await mcp_operational_report(),
            headers={"Cache-Control": "no-store"},
        )

    @server.custom_route(
        "/internal/mcp/audit", methods=["GET"], include_in_schema=False
    )
    async def internal_mcp_audit(request: Request) -> JSONResponse:
        denied = require_internal_observability(request)
        if denied is not None:
            return denied
        try:
            limit = min(
                runtime.max_page_size,
                max(1, int(request.query_params.get("limit", "100"))),
            )
            offset = _cursor_offset(request.query_params.get("cursor"))
        except (TypeError, ValueError, ToolError):
            return JSONResponse(
                {"error": "invalid_pagination"},
                status_code=400,
                headers={"Cache-Control": "no-store"},
            )
        tenant_id = request.query_params.get("tenant_id") or None
        if tenant_id is not None and len(tenant_id) > 200:
            return JSONResponse(
                {"error": "invalid_tenant"},
                status_code=400,
                headers={"Cache-Control": "no-store"},
            )
        values = await asyncio.to_thread(
            app.store.list_mcp_audit_events,
            tenant_id=tenant_id,
            limit=limit + 1,
            offset=offset,
        )
        payload = _bounded_page(
            values,
            key="audit_events",
            offset=offset,
            requested_limit=limit,
            max_bytes=runtime.max_tool_output_bytes,
        )
        payload["scope"] = {"tenant_id": tenant_id or "all"}
        return JSONResponse(payload, headers={"Cache-Control": "no-store"})

    @server.tool(title="Check crawler configuration", annotations=ReadOnly)
    def crawler_doctor() -> dict[str, Any]:
        """Check required runtime configuration without contacting or disclosing secrets."""
        principal("crawler_doctor")
        return {
            **configuration_report(app.settings),
            "mcp_runtime": {
                "production": runtime.production,
                "authentication": runtime.auth_mode,
                "oauth_provider": runtime.oauth_provider,
                "tenant_mode": runtime.oauth_tenant_mode,
                "tenant_scoped": True,
                "synchronous_run_enabled": runtime.synchronous_run_enabled,
                "single_instance_storage": runtime.instance_count == 1,
            },
        }

    @server.tool(title="Describe crawler capabilities", annotations=ReadOnly)
    def crawler_capabilities() -> dict[str, Any]:
        """Return crawler and MCP protocol capabilities."""
        principal("crawler_capabilities")
        return {
            "ok": True,
            **app.service.capabilities(),
            "mcp": {
                "latest_protocol_version": LATEST_PROTOCOL_VERSION,
                "known_protocol_versions": list(KNOWN_PROTOCOL_VERSIONS),
                "standard_interfaces": [
                    "initialize",
                    "tools/list",
                    "tools/call",
                    "resources/list",
                    "resources/templates/list",
                    "resources/read",
                ],
                "transport": ["stdio", "streamable-http"],
                "authentication": {
                    "mode": runtime.auth_mode,
                    "provider": runtime.oauth_provider,
                    "tenant_mode": runtime.oauth_tenant_mode,
                },
                "backend_controls": [
                    "tenant-scope",
                    "authorization-scope",
                    "rate-limit",
                    "concurrency-gate",
                    "approval-receipt",
                    "pagination",
                    "output-limit",
                    "upstream-circuit-breaker",
                ],
            },
        }

    @server.tool(title="Check crawler operational health", annotations=ReadOnly)
    async def crawler_health() -> dict[str, Any]:
        """Return only the minimum business readiness needed before creating work."""
        principal("crawler_health")
        report = await mcp_operational_report()
        return {
            "ok": report["ok"],
            "status": report["status"],
            "accepting_jobs": report["ok"],
        }

    @server.tool(title="List crawl jobs", annotations=ReadOnly)
    async def crawler_list_jobs(
        limit: Annotated[int, Field(ge=1, le=200)] = 50,
        status: TaskStatus | None = None,
        cursor: Annotated[str | None, Field(max_length=12)] = None,
    ) -> dict[str, Any]:
        """List recent jobs for the authenticated tenant."""
        current = principal("crawler_list_jobs")
        offset = _cursor_offset(cursor)
        page_limit = min(limit, runtime.max_page_size)
        jobs = await asyncio.to_thread(
            app.store.list_jobs,
            limit=page_limit + 1,
            status=status,
            tenant_id=current.tenant_id,
            offset=offset,
        )
        return _bounded_page(
            jobs,
            key="jobs",
            offset=offset,
            requested_limit=page_limit,
            max_bytes=runtime.max_tool_output_bytes,
        )

    @server.tool(title="Get a crawl job", annotations=ReadOnly)
    async def crawler_get_job(
        job_id: Annotated[str, Field(min_length=1, max_length=64)],
    ) -> dict[str, Any]:
        """Get one tenant-owned job and its durable item states."""
        current = principal("crawler_get_job")
        try:
            job = await asyncio.to_thread(
                app.store.get_job, job_id, tenant_id=current.tenant_id
            )
        except Exception as exc:
            _raise_public_tool_error(exc)
        return {"ok": True, "job": _bounded_job(job, runtime.max_tool_output_bytes)}

    async def paged_job_values(
        *,
        tool_name: str,
        job_id: str,
        limit: int,
        cursor: str | None,
        key: str,
        loader: Any,
    ) -> dict[str, Any]:
        current = principal(tool_name)
        offset = _cursor_offset(cursor)
        page_limit = min(limit, runtime.max_page_size)
        try:
            values = await asyncio.to_thread(
                loader,
                job_id,
                limit=page_limit + 1,
                offset=offset,
                tenant_id=current.tenant_id,
            )
        except Exception as exc:
            _raise_public_tool_error(exc)
        return _bounded_page(
            values,
            key=key,
            offset=offset,
            requested_limit=page_limit,
            max_bytes=runtime.max_tool_output_bytes,
        )

    @server.tool(title="Get crawl results", annotations=ReadOnly)
    async def crawler_get_results(
        job_id: Annotated[str, Field(min_length=1, max_length=64)],
        limit: Annotated[int, Field(ge=1, le=500)] = 100,
        cursor: Annotated[str | None, Field(max_length=12)] = None,
    ) -> dict[str, Any]:
        """Return a bounded page of canonical results."""
        return await paged_job_values(
            tool_name="crawler_get_results",
            job_id=job_id,
            limit=limit,
            cursor=cursor,
            key="results",
            loader=app.store.list_results,
        )

    @server.tool(title="Get crawl events", annotations=ReadOnly)
    async def crawler_get_events(
        job_id: Annotated[str, Field(min_length=1, max_length=64)],
        limit: Annotated[int, Field(ge=1, le=500)] = 100,
        cursor: Annotated[str | None, Field(max_length=12)] = None,
    ) -> dict[str, Any]:
        """Return a bounded event page explaining job behavior."""
        return await paged_job_values(
            tool_name="crawler_get_events",
            job_id=job_id,
            limit=limit,
            cursor=cursor,
            key="events",
            loader=app.store.list_events,
        )

    @server.tool(title="Get result deliveries", annotations=ReadOnly)
    async def crawler_get_deliveries(
        job_id: Annotated[str, Field(min_length=1, max_length=64)],
        limit: Annotated[int, Field(ge=1, le=500)] = 100,
        cursor: Annotated[str | None, Field(max_length=12)] = None,
    ) -> dict[str, Any]:
        """Return a bounded page of secondary-store deliveries."""
        return await paged_job_values(
            tool_name="crawler_get_deliveries",
            job_id=job_id,
            limit=limit,
            cursor=cursor,
            key="deliveries",
            loader=app.store.list_deliveries,
        )

    async def create_job(
        *, action: str, arguments: dict[str, Any], approval_receipt: str | None
    ) -> tuple[dict[str, Any], bool, MCPPrincipal]:
        _require_runtime_configuration(app)
        current = principal(action)
        selected = arguments.get("result_sinks")
        external = external_write_requested(
            selected if isinstance(selected, list) else None
        )
        if external:
            verify_approval(
                approval_receipt,
                current=current,
                action=action,
                arguments=arguments,
            )
        try:
            job, created = await asyncio.to_thread(
                app.service.create_job,
                inputs=arguments["inputs"],
                kind=arguments["kind"],
                marketplace_id=arguments["marketplace_id"],
                postal_code=arguments["postal_code"],
                execution_mode=arguments["execution_mode"],
                priority=arguments["priority"],
                max_attempts=arguments["max_attempts"],
                idempotency_key=arguments["idempotency_key"],
                options=_task_options(
                    arguments["result_sinks"],
                    arguments["tags"],
                    arguments["requested_fields"],
                ),
                external_result_write_authorized=external,
                tenant_id=current.tenant_id,
                created_by=current.actor_id,
            )
        except Exception as exc:
            _raise_public_tool_error(exc)
        return job, created, current

    @server.tool(title="Create a queued crawl job", annotations=CreatesWork)
    async def crawler_create_job(
        inputs: TaskInputs,
        kind: Annotated[str, Field(min_length=1, max_length=80)] = "amazon.product",
        marketplace_id: Annotated[str | None, Field(max_length=8)] = None,
        postal_code: Annotated[str | None, Field(max_length=32)] = None,
        execution_mode: Literal["standard", "overseas", "realtime"] = "standard",
        priority: Annotated[int, Field(ge=-100, le=100)] = 0,
        max_attempts: Annotated[int | None, Field(ge=1, le=20)] = None,
        idempotency_key: Annotated[str | None, Field(max_length=200)] = None,
        result_sinks: Annotated[list[str] | None, Field(max_length=8)] = None,
        tags: Annotated[list[str] | None, Field(max_length=20)] = None,
        requested_fields: Annotated[list[str] | None, Field(max_length=50)] = None,
        approval_receipt: Annotated[str | None, Field(max_length=4096)] = None,
    ) -> dict[str, Any]:
        """Persist an idempotent job; an external result sink requires approval."""
        arguments = _job_arguments(
            inputs=inputs,
            kind=kind,
            marketplace_id=marketplace_id,
            postal_code=postal_code,
            execution_mode=execution_mode,
            priority=priority,
            max_attempts=max_attempts,
            idempotency_key=idempotency_key,
            result_sinks=result_sinks,
            tags=tags,
            requested_fields=requested_fields,
        )
        job, created, _ = await create_job(
            action="crawler_create_job",
            arguments=arguments,
            approval_receipt=approval_receipt,
        )
        return {
            "ok": True,
            "created": created,
            "completion_evidence": False,
            "job": _bounded_job(job, runtime.max_tool_output_bytes),
            "next_action": "poll crawler_get_job, then call crawler_get_results",
        }

    @server.tool(title="Run and verify a crawl job", annotations=CreatesWork)
    async def crawler_run_job(
        inputs: TaskInputs,
        kind: Annotated[str, Field(min_length=1, max_length=80)] = "amazon.product",
        marketplace_id: Annotated[str | None, Field(max_length=8)] = None,
        postal_code: Annotated[str | None, Field(max_length=32)] = None,
        execution_mode: Literal["standard", "overseas", "realtime"] = "standard",
        priority: Annotated[int, Field(ge=-100, le=100)] = 0,
        max_attempts: Annotated[int | None, Field(ge=1, le=20)] = None,
        idempotency_key: Annotated[str | None, Field(max_length=200)] = None,
        result_sinks: Annotated[list[str] | None, Field(max_length=8)] = None,
        tags: Annotated[list[str] | None, Field(max_length=20)] = None,
        requested_fields: Annotated[list[str] | None, Field(max_length=50)] = None,
        timeout_seconds: Annotated[float, Field(ge=1, le=3600)] = 600.0,
        approval_receipt: Annotated[str | None, Field(max_length=4096)] = None,
    ) -> dict[str, Any]:
        """Create and execute one scoped job; production may require queued execution."""
        if not runtime.synchronous_run_enabled:
            raise ToolError(
                "synchronous MCP runs are disabled; use crawler_create_job and a worker"
            )
        arguments = _job_arguments(
            inputs=inputs,
            kind=kind,
            marketplace_id=marketplace_id,
            postal_code=postal_code,
            execution_mode=execution_mode,
            priority=priority,
            max_attempts=max_attempts,
            idempotency_key=idempotency_key,
            result_sinks=result_sinks,
            tags=tags,
            requested_fields=requested_fields,
            timeout_seconds=timeout_seconds,
        )
        job, created, current = await create_job(
            action="crawler_run_job",
            arguments=arguments,
            approval_receipt=approval_receipt,
        )
        try:
            report = await run_scoped_job(
                app,
                job["id"],
                timeout_seconds=timeout_seconds,
                result_limit_per_job=runtime.max_page_size,
                tenant_id=current.tenant_id,
            )
        except Exception as exc:
            _raise_public_tool_error(exc)
        return _bounded_run_report(
            {"ok": report["completed"], "created": created, **report},
            runtime.max_tool_output_bytes,
        )

    @server.tool(title="Pause a crawl job", annotations=ControlsWork)
    async def crawler_pause_job(
        job_id: Annotated[str, Field(min_length=1, max_length=64)],
    ) -> dict[str, Any]:
        """Request a reversible pause for one tenant-owned job."""
        current = principal("crawler_pause_job")
        try:
            job = await asyncio.to_thread(
                app.store.request_pause, job_id, tenant_id=current.tenant_id
            )
        except Exception as exc:
            _raise_public_tool_error(exc)
        return {"ok": True, "job": _bounded_job(job, runtime.max_tool_output_bytes)}

    @server.tool(title="Resume a crawl job", annotations=ControlsWork)
    async def crawler_resume_job(
        job_id: Annotated[str, Field(min_length=1, max_length=64)],
    ) -> dict[str, Any]:
        """Resume one tenant-owned paused job."""
        current = principal("crawler_resume_job")
        try:
            job = await asyncio.to_thread(
                app.store.resume, job_id, tenant_id=current.tenant_id
            )
        except Exception as exc:
            _raise_public_tool_error(exc)
        return {"ok": True, "job": _bounded_job(job, runtime.max_tool_output_bytes)}

    @server.tool(title="Cancel a crawl job", annotations=CancelsWork)
    async def crawler_cancel_job(
        job_id: Annotated[str, Field(min_length=1, max_length=64)],
        confirm: bool = False,
        approval_receipt: Annotated[str | None, Field(max_length=4096)] = None,
    ) -> dict[str, Any]:
        """Cancel a tenant-owned job with explicit local or signed production approval."""
        current = principal("crawler_cancel_job")
        if runtime.production:
            verify_approval(
                approval_receipt,
                current=current,
                action="crawler_cancel_job",
                arguments={"job_id": job_id},
            )
        elif not confirm:
            raise ToolError(
                "cancellation requires explicit user confirmation: confirm=true"
            )
        try:
            job = await asyncio.to_thread(
                app.store.request_cancel, job_id, tenant_id=current.tenant_id
            )
        except Exception as exc:
            _raise_public_tool_error(exc)
        return {"ok": True, "job": _bounded_job(job, runtime.max_tool_output_bytes)}

    @server.resource(
        "crawler://capabilities",
        title="Crawler capabilities",
        description="Crawler capability description.",
        mime_type="application/json",
    )
    def capabilities_resource() -> str:
        principal("crawler_capabilities")
        return json.dumps(
            app.service.capabilities(), ensure_ascii=False, sort_keys=True
        )

    @server.resource(
        "crawler://jobs/{job_id}",
        title="Crawl job",
        description="One tenant-owned durable crawl job.",
        mime_type="application/json",
    )
    def job_resource(job_id: str) -> str:
        current = principal("crawler_get_job")
        try:
            job = app.store.get_job(job_id, tenant_id=current.tenant_id)
        except Exception as exc:
            _raise_public_tool_error(exc)
        return json.dumps(
            _bounded_job(job, runtime.max_tool_output_bytes),
            ensure_ascii=False,
            sort_keys=True,
        )

    @server.resource(
        "crawler://jobs/{job_id}/results",
        title="Crawl job results",
        description="A bounded first page of canonical results.",
        mime_type="application/json",
    )
    def results_resource(job_id: str) -> str:
        current = principal("crawler_get_results")
        try:
            values = app.store.list_results(
                job_id,
                limit=min(100, runtime.max_page_size) + 1,
                tenant_id=current.tenant_id,
            )
        except Exception as exc:
            _raise_public_tool_error(exc)
        return json.dumps(
            _bounded_page(
                values,
                key="results",
                offset=0,
                requested_limit=min(100, runtime.max_page_size),
                max_bytes=runtime.max_tool_output_bytes,
            ),
            ensure_ascii=False,
            sort_keys=True,
        )

    return server


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="amazon-crawler-mcp")
    parser.add_argument("--db", type=Path, help="override CRAWLER_DB_PATH")
    parser.add_argument(
        "--transport", choices=["stdio", "streamable-http"], default="stdio"
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--path", default="/mcp")
    parser.add_argument("--json-response", action="store_true")
    parser.add_argument("--stateless-http", action="store_true")
    return parser


@contextmanager
def _single_instance_lock(
    settings: Settings, runtime: MCPRuntimeConfig
) -> Iterator[None]:
    if not runtime.production:
        yield
        return
    try:
        import fcntl
    except ImportError as exc:  # pragma: no cover - production guard for non-POSIX
        raise SystemExit("production SQLite MCP requires a POSIX process lock") from exc
    lock_path = settings.db_path.with_suffix(settings.db_path.suffix + ".mcp.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SystemExit(
                f"another production MCP Server already owns {lock_path}"
            ) from exc
        yield
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def main() -> None:
    args = _parser().parse_args()
    runtime = MCPRuntimeConfig.from_env()
    if args.transport == "stdio" and runtime.production and not runtime.local_scopes:
        raise SystemExit("production STDIO requires CRAWLER_MCP_LOCAL_SCOPES")
    if args.transport == "streamable-http":
        runtime.validate_http(args.host)
    app_settings = _settings(args.db)
    with _single_instance_lock(app_settings, runtime):
        server = create_mcp_server(settings=app_settings, runtime_config=runtime)
        try:
            if args.transport == "stdio":
                server.run("stdio")
                return
            allowed_hosts = list(runtime.allowed_hosts) or [f"{args.host}:{args.port}"]
            transport_security = TransportSecuritySettings(
                enable_dns_rebinding_protection=True,
                allowed_hosts=allowed_hosts,
                allowed_origins=list(runtime.allowed_origins),
            )
            server.run(
                "streamable-http",
                host=args.host,
                port=args.port,
                streamable_http_path=args.path,
                json_response=args.json_response,
                stateless_http=args.stateless_http or runtime.production,
                transport_security=transport_security,
            )
        except KeyboardInterrupt:
            return


if __name__ == "__main__":
    main()
