from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.context import CallNext, HandlerResult, ServerRequestContext
from mcp.shared.exceptions import MCPError

from amazon_crawler.infra.sqlite_store import SQLiteStore

SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,127}$")


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _csv(value: str | None) -> tuple[str, ...]:
    return tuple(item.strip() for item in (value or "").split(",") if item.strip())


def arguments_sha256(value: Mapping[str, Any]) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class StaticTokenRecord:
    digest: str
    client_id: str
    actor_id: str
    tenant_id: str
    scopes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MCPRuntimeConfig:
    production: bool
    auth_mode: str
    issuer_url: str | None
    resource_server_url: str | None
    token_records: tuple[StaticTokenRecord, ...] = field(repr=False)
    local_tenant_id: str = "local"
    local_actor_id: str = "local-operator"
    local_client_id: str = "stdio"
    local_scopes: tuple[str, ...] = ("*",)
    allowed_hosts: tuple[str, ...] = ()
    allowed_origins: tuple[str, ...] = ()
    rate_limit_per_minute: int = 120
    rate_limit_burst: int = 20
    max_concurrent_runs: int = 2
    run_queue_timeout_seconds: float = 0.25
    synchronous_run_enabled: bool = True
    instance_count: int = 1
    max_page_size: int = 100
    max_tool_output_bytes: int = 500_000
    approval_signing_key: str | None = field(default=None, repr=False)
    approval_ttl_seconds: int = 300
    audit_retention_days: int = 30

    @classmethod
    def from_env(cls) -> MCPRuntimeConfig:
        auth_mode = os.getenv("CRAWLER_MCP_AUTH_MODE", "disabled").strip().lower()
        if auth_mode not in {"disabled", "static"}:
            raise ValueError("CRAWLER_MCP_AUTH_MODE must be disabled or static")
        records: list[StaticTokenRecord] = []
        raw_records = os.getenv("CRAWLER_MCP_STATIC_TOKENS_SHA256_JSON", "").strip()
        if raw_records:
            payload = json.loads(raw_records)
            if not isinstance(payload, dict):
                raise ValueError(
                    "CRAWLER_MCP_STATIC_TOKENS_SHA256_JSON must be an object"
                )
            for digest, raw in payload.items():
                if not isinstance(digest, str) or not re.fullmatch(
                    r"[0-9a-fA-F]{64}", digest
                ):
                    raise ValueError(
                        "MCP static token keys must be SHA-256 hex digests"
                    )
                if not isinstance(raw, dict):
                    raise ValueError("each MCP static token record must be an object")
                client_id = str(raw.get("client_id") or "").strip()
                actor_id = str(raw.get("actor_id") or raw.get("subject") or "").strip()
                tenant_id = str(raw.get("tenant_id") or "").strip()
                scopes = raw.get("scopes")
                if not all(
                    SAFE_IDENTIFIER.fullmatch(value or "")
                    for value in (client_id, actor_id, tenant_id)
                ):
                    raise ValueError(
                        "MCP token identity fields contain invalid characters"
                    )
                if (
                    not isinstance(scopes, list)
                    or not scopes
                    or not all(
                        isinstance(scope, str) and SAFE_IDENTIFIER.fullmatch(scope)
                        for scope in scopes
                    )
                ):
                    raise ValueError("MCP token scopes must be a non-empty string list")
                records.append(
                    StaticTokenRecord(
                        digest=digest.lower(),
                        client_id=client_id,
                        actor_id=actor_id,
                        tenant_id=tenant_id,
                        scopes=tuple(scopes),
                    )
                )
        if auth_mode == "static" and not records:
            raise ValueError(
                "static MCP authentication requires at least one token digest"
            )

        production = _as_bool(os.getenv("CRAWLER_MCP_PRODUCTION"), False)
        local_scopes = _csv(os.getenv("CRAWLER_MCP_LOCAL_SCOPES")) or (
            ("*",) if not production else ()
        )
        config = cls(
            production=production,
            auth_mode=auth_mode,
            issuer_url=os.getenv("CRAWLER_MCP_ISSUER_URL") or None,
            resource_server_url=os.getenv("CRAWLER_MCP_RESOURCE_SERVER_URL") or None,
            token_records=tuple(records),
            local_tenant_id=os.getenv("CRAWLER_MCP_LOCAL_TENANT_ID", "local").strip(),
            local_actor_id=os.getenv(
                "CRAWLER_MCP_LOCAL_ACTOR_ID", "local-operator"
            ).strip(),
            local_client_id=os.getenv("CRAWLER_MCP_LOCAL_CLIENT_ID", "stdio").strip(),
            local_scopes=local_scopes,
            allowed_hosts=_csv(os.getenv("CRAWLER_MCP_ALLOWED_HOSTS")),
            allowed_origins=_csv(os.getenv("CRAWLER_MCP_ALLOWED_ORIGINS")),
            rate_limit_per_minute=max(
                1, int(os.getenv("CRAWLER_MCP_RATE_LIMIT_PER_MINUTE", "120"))
            ),
            rate_limit_burst=max(
                1, int(os.getenv("CRAWLER_MCP_RATE_LIMIT_BURST", "20"))
            ),
            max_concurrent_runs=max(
                1, int(os.getenv("CRAWLER_MCP_MAX_CONCURRENT_RUNS", "2"))
            ),
            run_queue_timeout_seconds=max(
                0.01,
                float(os.getenv("CRAWLER_MCP_RUN_QUEUE_TIMEOUT_SECONDS", "0.25")),
            ),
            synchronous_run_enabled=_as_bool(
                os.getenv("CRAWLER_MCP_SYNC_RUN_ENABLED"),
                not production,
            ),
            instance_count=max(1, int(os.getenv("CRAWLER_MCP_INSTANCE_COUNT", "1"))),
            max_page_size=max(
                1, min(500, int(os.getenv("CRAWLER_MCP_MAX_PAGE_SIZE", "100")))
            ),
            max_tool_output_bytes=max(
                10_000,
                int(os.getenv("CRAWLER_MCP_MAX_TOOL_OUTPUT_BYTES", "500000")),
            ),
            approval_signing_key=os.getenv("CRAWLER_MCP_APPROVAL_SIGNING_KEY") or None,
            approval_ttl_seconds=max(
                30,
                min(3600, int(os.getenv("CRAWLER_MCP_APPROVAL_TTL_SECONDS", "300"))),
            ),
            audit_retention_days=max(
                1,
                min(3650, int(os.getenv("CRAWLER_MCP_AUDIT_RETENTION_DAYS", "30"))),
            ),
        )
        for value in (
            config.local_tenant_id,
            config.local_actor_id,
            config.local_client_id,
        ):
            if not SAFE_IDENTIFIER.fullmatch(value):
                raise ValueError("MCP local identity fields contain invalid characters")
        if production and config.instance_count != 1:
            raise ValueError(
                "the SQLite MCP deployment is single-instance; set "
                "CRAWLER_MCP_INSTANCE_COUNT=1 or use a shared-store implementation"
            )
        if (
            config.approval_signing_key
            and len(config.approval_signing_key.encode("utf-8")) < 32
        ):
            raise ValueError(
                "CRAWLER_MCP_APPROVAL_SIGNING_KEY must be at least 32 bytes"
            )
        if config.auth_mode != "disabled" and (
            not config.issuer_url or not config.resource_server_url
        ):
            raise ValueError(
                "authenticated MCP requires issuer and resource-server URLs"
            )
        return config

    def validate_http(self, host: str) -> None:
        loopback = host in {"127.0.0.1", "localhost", "::1"}
        if not loopback and self.auth_mode == "disabled":
            raise ValueError("non-loopback MCP HTTP requires authentication")
        if not loopback and (not self.allowed_hosts or not self.allowed_origins):
            raise ValueError(
                "non-loopback MCP HTTP requires CRAWLER_MCP_ALLOWED_HOSTS and "
                "CRAWLER_MCP_ALLOWED_ORIGINS"
            )
        if self.production and self.auth_mode == "disabled":
            raise ValueError("production MCP HTTP requires authentication")


class StaticSHA256TokenVerifier(TokenVerifier):
    def __init__(self, records: tuple[StaticTokenRecord, ...]) -> None:
        self._records = records

    async def verify_token(self, token: str) -> AccessToken | None:
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        selected: StaticTokenRecord | None = None
        for record in self._records:
            if hmac.compare_digest(digest, record.digest):
                selected = record
        if selected is None:
            return None
        return AccessToken(
            token=token,
            client_id=selected.client_id,
            subject=selected.actor_id,
            scopes=list(selected.scopes),
            claims={"tenant_id": selected.tenant_id},
        )


@dataclass(frozen=True, slots=True)
class MCPPrincipal:
    tenant_id: str
    actor_id: str
    client_id: str
    scopes: frozenset[str]

    def permits(self, scope: str) -> bool:
        return "*" in self.scopes or scope in self.scopes


class TokenBucketRateLimiter:
    def __init__(self, per_minute: int, burst: int) -> None:
        self._rate = max(1, per_minute) / 60.0
        self._capacity = float(max(1, burst))
        self._state: dict[str, tuple[float, float]] = {}
        self._lock = asyncio.Lock()

    async def consume(self, key: str) -> float | None:
        async with self._lock:
            now = time.monotonic()
            tokens, updated = self._state.get(key, (self._capacity, now))
            tokens = min(self._capacity, tokens + ((now - updated) * self._rate))
            if tokens < 1.0:
                self._state[key] = (tokens, now)
                return round((1.0 - tokens) / self._rate, 3)
            self._state[key] = (tokens - 1.0, now)
            return None


class RunConcurrencyGate:
    def __init__(self, capacity: int, queue_timeout_seconds: float) -> None:
        self._semaphore = asyncio.Semaphore(max(1, capacity))
        self._timeout = max(0.01, queue_timeout_seconds)

    async def run(
        self, operation: Callable[[], Awaitable[HandlerResult]]
    ) -> HandlerResult:
        try:
            await asyncio.wait_for(self._semaphore.acquire(), timeout=self._timeout)
        except TimeoutError as exc:
            raise MCPError(
                -32003,
                "crawler run concurrency is full; retry later or queue with crawler_create_job",
            ) from exc
        try:
            return await operation()
        finally:
            self._semaphore.release()


TOOL_SCOPES = {
    "crawler_doctor": "crawler:read",
    "crawler_capabilities": "crawler:read",
    "crawler_list_jobs": "crawler:read",
    "crawler_get_job": "crawler:read",
    "crawler_get_results": "crawler:read",
    "crawler_get_events": "crawler:read",
    "crawler_get_deliveries": "crawler:read",
    "crawler_metrics": "crawler:read",
    "crawler_create_job": "crawler:run",
    "crawler_run_job": "crawler:run",
    "crawler_pause_job": "crawler:control",
    "crawler_resume_job": "crawler:control",
    "crawler_cancel_job": "crawler:cancel",
    "crawler_get_audit_events": "crawler:audit",
}


class MCPExecutionPolicy:
    def __init__(self, config: MCPRuntimeConfig, store: SQLiteStore) -> None:
        self.config = config
        self.store = store
        self.rate_limiter = TokenBucketRateLimiter(
            config.rate_limit_per_minute,
            config.rate_limit_burst,
        )
        self.run_gate = RunConcurrencyGate(
            config.max_concurrent_runs,
            config.run_queue_timeout_seconds,
        )
        self.store.prune_mcp_security_state(
            audit_retention_days=config.audit_retention_days
        )

    def principal(self) -> MCPPrincipal:
        access_token = get_access_token()
        if access_token is None:
            return MCPPrincipal(
                tenant_id=self.config.local_tenant_id,
                actor_id=self.config.local_actor_id,
                client_id=self.config.local_client_id,
                scopes=frozenset(self.config.local_scopes),
            )
        claims = access_token.claims or {}
        tenant_id = str(claims.get("tenant_id") or "").strip()
        actor_id = str(access_token.subject or access_token.client_id).strip()
        if not SAFE_IDENTIFIER.fullmatch(tenant_id) or not SAFE_IDENTIFIER.fullmatch(
            actor_id
        ):
            raise MCPError(
                -32001, "authenticated identity is missing a valid tenant or actor"
            )
        return MCPPrincipal(
            tenant_id=tenant_id,
            actor_id=actor_id,
            client_id=access_token.client_id,
            scopes=frozenset(access_token.scopes),
        )

    def require_scope(self, principal: MCPPrincipal, tool_name: str) -> None:
        scope = TOOL_SCOPES.get(tool_name)
        if scope is None:
            raise MCPError(-32001, "tool is not present in the authorization policy")
        if not principal.permits(scope):
            raise MCPError(-32001, f"missing required scope: {scope}")

    async def __call__(
        self,
        ctx: ServerRequestContext[Any, Any],
        call_next: CallNext,
    ) -> HandlerResult:
        governed_methods = {
            "tools/call",
            "resources/list",
            "resources/templates/list",
            "resources/read",
        }
        if ctx.method not in governed_methods:
            return await call_next(ctx)
        if isinstance(ctx.params, Mapping):
            params = dict(ctx.params)
        elif hasattr(ctx.params, "model_dump"):
            params = ctx.params.model_dump(mode="json", by_alias=True)
        else:
            params = {}
        tool_name = (
            str(params.get("name") or "")
            if ctx.method == "tools/call"
            else f"mcp:{ctx.method}"
        )
        raw_arguments = params.get("arguments")
        arguments = (
            raw_arguments
            if ctx.method == "tools/call" and isinstance(raw_arguments, Mapping)
            else params
            if ctx.method != "tools/call"
            else {}
        )
        principal = self.principal()
        audit_id = f"audit_{uuid.uuid4().hex}"
        started = time.monotonic()
        await asyncio.to_thread(
            self.store.start_mcp_audit,
            audit_id=audit_id,
            tenant_id=principal.tenant_id,
            actor_id=principal.actor_id,
            client_id=principal.client_id,
            tool_name=tool_name,
            arguments_sha256=arguments_sha256(arguments),
            request_id=str(ctx.request_id) if ctx.request_id is not None else None,
        )
        status = "failed"
        error_code: str | None = None
        try:
            if ctx.method == "tools/call":
                self.require_scope(principal, tool_name)
            elif not principal.permits("crawler:read"):
                raise MCPError(-32001, "missing required scope: crawler:read")
            retry_after = await self.rate_limiter.consume(
                f"{principal.tenant_id}:{principal.actor_id}"
            )
            if retry_after is not None:
                error_code = "rate_limited"
                raise MCPError(
                    -32002,
                    "MCP rate limit exceeded",
                    {"retry_after_seconds": retry_after},
                )

            async def invoke() -> HandlerResult:
                return await call_next(ctx)

            result = (
                await self.run_gate.run(invoke)
                if ctx.method == "tools/call" and tool_name == "crawler_run_job"
                else await invoke()
            )
            rendered = (
                result.model_dump(mode="json", by_alias=True)
                if hasattr(result, "model_dump")
                else result
            )
            failed = isinstance(rendered, dict) and bool(
                rendered.get("isError") or rendered.get("is_error")
            )
            status = "failed" if failed else "succeeded"
            error_code = "tool_error" if failed else None
            return result
        except MCPError as exc:
            error_code = error_code or f"mcp_{exc.code}"
            raise
        except Exception:
            error_code = "internal_error"
            raise
        finally:
            latency_ms = int((time.monotonic() - started) * 1000)
            await asyncio.to_thread(
                self.store.finish_mcp_audit,
                audit_id,
                status=status,
                latency_ms=latency_ms,
                error_code=error_code,
            )


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + ("=" * (-len(value) % 4)))


class ApprovalAuthority:
    def __init__(self, config: MCPRuntimeConfig, store: SQLiteStore) -> None:
        self.config = config
        self.store = store

    def issue(
        self,
        *,
        principal: MCPPrincipal,
        action: str,
        arguments: Mapping[str, Any],
        ttl_seconds: int | None = None,
    ) -> str:
        key = self.config.approval_signing_key
        if not key:
            raise ValueError("CRAWLER_MCP_APPROVAL_SIGNING_KEY is not configured")
        ttl = max(30, min(ttl_seconds or self.config.approval_ttl_seconds, 3600))
        expires_at = datetime.now(UTC) + timedelta(seconds=ttl)
        payload = {
            "v": 1,
            "nonce": uuid.uuid4().hex,
            "tenant_id": principal.tenant_id,
            "actor_id": principal.actor_id,
            "action": action,
            "parameters_sha256": arguments_sha256(arguments),
            "expires_at": expires_at.isoformat(),
        }
        encoded = _b64encode(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        )
        signature = _b64encode(
            hmac.new(
                key.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256
            ).digest()
        )
        return f"{encoded}.{signature}"

    def verify_and_consume(
        self,
        receipt: str | None,
        *,
        principal: MCPPrincipal,
        action: str,
        arguments: Mapping[str, Any],
    ) -> None:
        key = self.config.approval_signing_key
        if not key:
            raise MCPError(-32004, "approval signing key is not configured")
        if not receipt or "." not in receipt:
            raise MCPError(-32004, "a verified approval receipt is required")
        encoded, supplied_signature = receipt.split(".", 1)
        expected_signature = _b64encode(
            hmac.new(
                key.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256
            ).digest()
        )
        if not hmac.compare_digest(supplied_signature, expected_signature):
            raise MCPError(-32004, "approval receipt signature is invalid")
        try:
            payload = json.loads(_b64decode(encoded))
            expires_at = datetime.fromisoformat(str(payload["expires_at"]))
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise MCPError(-32004, "approval receipt payload is invalid") from exc
        expected_hash = arguments_sha256(arguments)
        nonce = str(payload.get("nonce") or "")
        if (
            payload.get("v") != 1
            or not re.fullmatch(r"[0-9a-f]{32}", nonce)
            or payload.get("tenant_id") != principal.tenant_id
            or payload.get("actor_id") != principal.actor_id
            or payload.get("action") != action
            or payload.get("parameters_sha256") != expected_hash
        ):
            raise MCPError(-32004, "approval receipt does not match this exact action")
        if expires_at.tzinfo is None or expires_at <= datetime.now(UTC):
            raise MCPError(-32004, "approval receipt has expired")
        self.store.consume_mcp_approval(
            nonce=nonce,
            tenant_id=principal.tenant_id,
            actor_id=principal.actor_id,
            action=action,
            parameters_sha256=expected_hash,
            expires_at=expires_at.isoformat(),
        )
