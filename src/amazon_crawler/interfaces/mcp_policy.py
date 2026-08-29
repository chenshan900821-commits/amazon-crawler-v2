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
from urllib.parse import urlsplit

import jwt
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.context import CallNext, HandlerResult, ServerRequestContext
from mcp.shared.exceptions import MCPError

from amazon_crawler.infra.sqlite_store import SQLiteStore
from amazon_crawler.interfaces.mcp_observability import MCPObservability

SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,127}$")
PRINCIPAL_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/|+\-=]{0,254}$")
OAUTH_ASYMMETRIC_ALGORITHMS = frozenset(
    {
        "RS256",
        "RS384",
        "RS512",
        "PS256",
        "PS384",
        "PS512",
        "ES256",
        "ES384",
        "ES512",
        "EdDSA",
    }
)


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
class TestTokenRecord:
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
    test_token_records: tuple[TestTokenRecord, ...] = field(repr=False)
    oauth_provider: str = "generic"
    oauth_jwks_url: str | None = None
    oauth_audience: str | None = None
    oauth_algorithms: tuple[str, ...] = ("RS256",)
    oauth_tenant_claim: str = "tenant_id"
    oauth_tenant_mode: str = "claim"
    oauth_scope_claim: str = "scope"
    oauth_permissions_claim: str | None = None
    oauth_allowed_tenants: tuple[str, ...] = ()
    oauth_require_at_jwt: bool = True
    oauth_clock_skew_seconds: int = 60
    oauth_jwks_timeout_seconds: float = 5.0
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
    metrics_window_seconds: int = 300
    metrics_max_samples: int = 10_000
    alert_min_calls: int = 20
    alert_error_rate: float = 0.25
    alert_p95_latency_ms: int = 5_000
    alert_rate_limit_count: int = 10
    alert_concurrency_rejection_count: int = 3
    alert_webhook_url: str | None = field(default=None, repr=False)
    alert_webhook_timeout_seconds: float = 3.0
    alert_webhook_max_attempts: int = 3
    internal_observability_enabled: bool = False
    internal_observability_token_sha256: str | None = field(default=None, repr=False)

    @classmethod
    def from_env(cls) -> MCPRuntimeConfig:
        auth_mode = os.getenv("CRAWLER_MCP_AUTH_MODE", "disabled").strip().lower()
        if auth_mode == "static":
            auth_mode = "test-token"
        if auth_mode not in {"disabled", "oauth", "test-token"}:
            raise ValueError(
                "CRAWLER_MCP_AUTH_MODE must be disabled, oauth, or test-token"
            )
        records: list[TestTokenRecord] = []
        raw_records = os.getenv(
            "CRAWLER_MCP_TEST_TOKENS_SHA256_JSON",
            os.getenv("CRAWLER_MCP_STATIC_TOKENS_SHA256_JSON", ""),
        ).strip()
        if raw_records:
            payload = json.loads(raw_records)
            if not isinstance(payload, dict):
                raise ValueError(
                    "CRAWLER_MCP_TEST_TOKENS_SHA256_JSON must be an object"
                )
            for digest, raw in payload.items():
                if not isinstance(digest, str) or not re.fullmatch(
                    r"[0-9a-fA-F]{64}", digest
                ):
                    raise ValueError("MCP test token keys must be SHA-256 hex digests")
                if not isinstance(raw, dict):
                    raise TypeError("each MCP test token record must be an object")
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
                        isinstance(scope, str)
                        and SAFE_IDENTIFIER.fullmatch(scope)
                        or scope == "*"
                        for scope in scopes
                    )
                ):
                    raise ValueError("MCP token scopes must be a non-empty string list")
                records.append(
                    TestTokenRecord(
                        digest=digest.lower(),
                        client_id=client_id,
                        actor_id=actor_id,
                        tenant_id=tenant_id,
                        scopes=tuple(scopes),
                    )
                )

        plaintext_test_token = os.getenv("CRAWLER_MCP_TEST_TOKEN", "").strip()
        if plaintext_test_token:
            test_client_id = os.getenv(
                "CRAWLER_MCP_TEST_CLIENT_ID", "test-client"
            ).strip()
            test_actor_id = os.getenv(
                "CRAWLER_MCP_TEST_ACTOR_ID", "test-operator"
            ).strip()
            test_tenant_id = os.getenv(
                "CRAWLER_MCP_TEST_TENANT_ID", "test-tenant"
            ).strip()
            test_scopes = _csv(os.getenv("CRAWLER_MCP_TEST_SCOPES")) or ("*",)
            if not all(
                PRINCIPAL_IDENTIFIER.fullmatch(value or "")
                for value in (test_client_id, test_actor_id, test_tenant_id)
            ):
                raise ValueError("MCP test-token identity fields are invalid")
            if not all(
                scope == "*" or SAFE_IDENTIFIER.fullmatch(scope)
                for scope in test_scopes
            ):
                raise ValueError("MCP test-token scopes are invalid")
            records.append(
                TestTokenRecord(
                    digest=hashlib.sha256(
                        plaintext_test_token.encode("utf-8")
                    ).hexdigest(),
                    client_id=test_client_id,
                    actor_id=test_actor_id,
                    tenant_id=test_tenant_id,
                    scopes=test_scopes,
                )
            )
        if auth_mode == "test-token" and not records:
            raise ValueError(
                "test-token MCP authentication requires CRAWLER_MCP_TEST_TOKEN "
                "or CRAWLER_MCP_TEST_TOKENS_SHA256_JSON"
            )

        production = _as_bool(os.getenv("CRAWLER_MCP_PRODUCTION"), False)
        if production and (auth_mode == "test-token" or records):
            raise ValueError(
                "test-token MCP authentication and token configuration are "
                "forbidden in production"
            )
        local_scopes = _csv(os.getenv("CRAWLER_MCP_LOCAL_SCOPES")) or (
            ("*",) if not production else ()
        )
        oauth_provider = (
            os.getenv("CRAWLER_MCP_OAUTH_PROVIDER", "generic").strip().lower()
        )
        if oauth_provider not in {"generic", "auth0"}:
            raise ValueError("CRAWLER_MCP_OAUTH_PROVIDER must be generic or auth0")
        issuer_url = os.getenv("CRAWLER_MCP_ISSUER_URL") or None
        resource_server_url = os.getenv("CRAWLER_MCP_RESOURCE_SERVER_URL") or None
        oauth_jwks_url = os.getenv("CRAWLER_MCP_OAUTH_JWKS_URL") or None
        if auth_mode == "oauth" and oauth_provider == "auth0":
            raw_auth0_domain = os.getenv("CRAWLER_MCP_AUTH0_DOMAIN", "").strip()
            if not raw_auth0_domain and issuer_url:
                raw_auth0_domain = urlsplit(issuer_url).netloc
            auth0_domain = raw_auth0_domain.removeprefix("https://").rstrip("/")
            parsed_auth0_domain = urlsplit(f"https://{auth0_domain}")
            if (
                not auth0_domain
                or parsed_auth0_domain.netloc != auth0_domain
                or parsed_auth0_domain.path
                or parsed_auth0_domain.query
                or parsed_auth0_domain.fragment
            ):
                raise ValueError(
                    "CRAWLER_MCP_AUTH0_DOMAIN must be a hostname without scheme or path"
                )
            expected_issuer = f"https://{auth0_domain}/"
            if issuer_url and issuer_url != expected_issuer:
                raise ValueError(
                    "CRAWLER_MCP_ISSUER_URL must exactly match the Auth0 issuer, "
                    "including its trailing slash"
                )
            issuer_url = expected_issuer
            oauth_jwks_url = oauth_jwks_url or (
                f"https://{auth0_domain}/.well-known/jwks.json"
            )
        oauth_audience = os.getenv("CRAWLER_MCP_OAUTH_AUDIENCE") or resource_server_url
        oauth_algorithms = _csv(os.getenv("CRAWLER_MCP_OAUTH_ALGORITHMS")) or ("RS256",)
        if not set(oauth_algorithms).issubset(OAUTH_ASYMMETRIC_ALGORITHMS):
            raise ValueError(
                "CRAWLER_MCP_OAUTH_ALGORITHMS may contain only approved "
                "asymmetric signing algorithms"
            )
        config = cls(
            production=production,
            auth_mode=auth_mode,
            issuer_url=issuer_url,
            resource_server_url=resource_server_url,
            test_token_records=tuple(records),
            oauth_provider=oauth_provider,
            oauth_jwks_url=oauth_jwks_url,
            oauth_audience=oauth_audience,
            oauth_algorithms=oauth_algorithms,
            oauth_tenant_claim=os.getenv(
                "CRAWLER_MCP_OAUTH_TENANT_CLAIM",
                "org_id" if oauth_provider == "auth0" else "tenant_id",
            ).strip(),
            oauth_tenant_mode=os.getenv(
                "CRAWLER_MCP_OAUTH_TENANT_MODE",
                "subject" if oauth_provider == "auth0" else "claim",
            )
            .strip()
            .lower(),
            oauth_scope_claim=os.getenv(
                "CRAWLER_MCP_OAUTH_SCOPE_CLAIM", "scope"
            ).strip(),
            oauth_permissions_claim=(
                os.getenv("CRAWLER_MCP_OAUTH_PERMISSIONS_CLAIM")
                or ("permissions" if oauth_provider == "auth0" else None)
            ),
            oauth_allowed_tenants=_csv(os.getenv("CRAWLER_MCP_OAUTH_ALLOWED_TENANTS")),
            oauth_require_at_jwt=_as_bool(
                os.getenv("CRAWLER_MCP_OAUTH_REQUIRE_AT_JWT"), True
            ),
            oauth_clock_skew_seconds=max(
                0,
                min(
                    300,
                    int(os.getenv("CRAWLER_MCP_OAUTH_CLOCK_SKEW_SECONDS", "60")),
                ),
            ),
            oauth_jwks_timeout_seconds=max(
                0.25,
                min(
                    30.0,
                    float(os.getenv("CRAWLER_MCP_OAUTH_JWKS_TIMEOUT_SECONDS", "5")),
                ),
            ),
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
            metrics_window_seconds=max(
                30,
                min(
                    3600,
                    int(os.getenv("CRAWLER_MCP_METRICS_WINDOW_SECONDS", "300")),
                ),
            ),
            metrics_max_samples=max(
                100,
                min(
                    100_000,
                    int(os.getenv("CRAWLER_MCP_METRICS_MAX_SAMPLES", "10000")),
                ),
            ),
            alert_min_calls=max(1, int(os.getenv("CRAWLER_MCP_ALERT_MIN_CALLS", "20"))),
            alert_error_rate=max(
                0.0,
                min(
                    1.0,
                    float(os.getenv("CRAWLER_MCP_ALERT_ERROR_RATE", "0.25")),
                ),
            ),
            alert_p95_latency_ms=max(
                1, int(os.getenv("CRAWLER_MCP_ALERT_P95_LATENCY_MS", "5000"))
            ),
            alert_rate_limit_count=max(
                1, int(os.getenv("CRAWLER_MCP_ALERT_RATE_LIMIT_COUNT", "10"))
            ),
            alert_concurrency_rejection_count=max(
                1,
                int(os.getenv("CRAWLER_MCP_ALERT_CONCURRENCY_REJECTION_COUNT", "3")),
            ),
            alert_webhook_url=os.getenv("CRAWLER_MCP_ALERT_WEBHOOK_URL") or None,
            alert_webhook_timeout_seconds=max(
                0.25,
                min(
                    30.0,
                    float(os.getenv("CRAWLER_MCP_ALERT_WEBHOOK_TIMEOUT_SECONDS", "3")),
                ),
            ),
            alert_webhook_max_attempts=max(
                1,
                min(
                    5,
                    int(os.getenv("CRAWLER_MCP_ALERT_WEBHOOK_MAX_ATTEMPTS", "3")),
                ),
            ),
            internal_observability_enabled=_as_bool(
                os.getenv("CRAWLER_MCP_INTERNAL_OBSERVABILITY_ENABLED"), False
            ),
            internal_observability_token_sha256=(
                os.getenv("CRAWLER_MCP_INTERNAL_OBSERVABILITY_TOKEN_SHA256", "")
                .strip()
                .lower()
                or None
            ),
        )
        for value in (
            config.local_tenant_id,
            config.local_actor_id,
            config.local_client_id,
        ):
            if not PRINCIPAL_IDENTIFIER.fullmatch(value):
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
        if config.auth_mode == "oauth":
            if not config.oauth_jwks_url or not config.oauth_audience:
                raise ValueError(
                    "OAuth MCP requires CRAWLER_MCP_OAUTH_JWKS_URL and an "
                    "audience (CRAWLER_MCP_OAUTH_AUDIENCE or resource-server URL)"
                )
            for claim_name in (
                config.oauth_tenant_claim,
                config.oauth_scope_claim,
            ):
                if not SAFE_IDENTIFIER.fullmatch(claim_name):
                    raise ValueError("OAuth MCP claim names are invalid")
            if config.oauth_permissions_claim and not SAFE_IDENTIFIER.fullmatch(
                config.oauth_permissions_claim
            ):
                raise ValueError("OAuth MCP permissions claim name is invalid")
            if config.oauth_tenant_mode not in {"claim", "subject"}:
                raise ValueError(
                    "CRAWLER_MCP_OAUTH_TENANT_MODE must be claim or subject"
                )
            if not all(
                PRINCIPAL_IDENTIFIER.fullmatch(tenant)
                for tenant in config.oauth_allowed_tenants
            ):
                raise ValueError("OAuth MCP allowed tenant identifiers are invalid")
            if (
                production
                and config.oauth_tenant_mode == "claim"
                and not config.oauth_allowed_tenants
            ):
                raise ValueError(
                    "production claim-based tenancy requires "
                    "CRAWLER_MCP_OAUTH_ALLOWED_TENANTS"
                )
            if config.oauth_provider == "auth0" and (
                config.oauth_algorithms != ("RS256",) or not config.oauth_require_at_jwt
            ):
                raise ValueError(
                    "the Auth0 P0 profile requires RS256 and RFC 9068 at+jwt tokens"
                )
            for name, url in (
                ("issuer", config.issuer_url),
                ("resource server", config.resource_server_url),
                ("JWKS", config.oauth_jwks_url),
            ):
                _validate_oauth_url(name, str(url), require_https=production)
        if config.alert_webhook_url:
            parsed_webhook = urlsplit(config.alert_webhook_url)
            loopback_webhook = parsed_webhook.hostname in {
                "127.0.0.1",
                "localhost",
                "::1",
            }
            if (
                not parsed_webhook.scheme
                or not parsed_webhook.hostname
                or parsed_webhook.fragment
                or parsed_webhook.username
                or parsed_webhook.password
                or parsed_webhook.scheme not in {"http", "https"}
                or (
                    parsed_webhook.scheme != "https"
                    and (production or not loopback_webhook)
                )
            ):
                raise ValueError(
                    "CRAWLER_MCP_ALERT_WEBHOOK_URL must use HTTPS; loopback HTTP "
                    "is allowed only outside production"
                )
        if config.internal_observability_token_sha256 and not re.fullmatch(
            r"[0-9a-f]{64}", config.internal_observability_token_sha256
        ):
            raise ValueError(
                "CRAWLER_MCP_INTERNAL_OBSERVABILITY_TOKEN_SHA256 must be a "
                "lowercase SHA-256 digest"
            )
        if (
            config.internal_observability_enabled
            and not config.internal_observability_token_sha256
        ):
            raise ValueError(
                "internal MCP observability requires "
                "CRAWLER_MCP_INTERNAL_OBSERVABILITY_TOKEN_SHA256"
            )
        return config

    def validate_http(self, host: str) -> None:
        loopback = host in {"127.0.0.1", "localhost", "::1"}
        if not loopback and self.auth_mode != "oauth":
            raise ValueError("non-loopback MCP HTTP requires OAuth authentication")
        if not loopback and (not self.allowed_hosts or not self.allowed_origins):
            raise ValueError(
                "non-loopback MCP HTTP requires CRAWLER_MCP_ALLOWED_HOSTS and "
                "CRAWLER_MCP_ALLOWED_ORIGINS"
            )
        if self.production and self.auth_mode != "oauth":
            raise ValueError("production MCP HTTP requires OAuth authentication")
        if not loopback and self.auth_mode == "oauth":
            for name, url in (
                ("issuer", self.issuer_url),
                ("resource server", self.resource_server_url),
                ("JWKS", self.oauth_jwks_url),
            ):
                _validate_oauth_url(name, str(url), require_https=True)


def _validate_oauth_url(name: str, value: str, *, require_https: bool) -> None:
    parsed = urlsplit(value)
    if not parsed.scheme or not parsed.hostname or parsed.fragment:
        raise ValueError(f"MCP OAuth {name} URL must be absolute and have no fragment")
    if name == "issuer" and parsed.query:
        raise ValueError("MCP OAuth issuer URL must not contain a query")
    if require_https and parsed.scheme != "https":
        raise ValueError(f"MCP OAuth {name} URL must use HTTPS")
    if parsed.scheme not in {"http", "https"}:
        raise ValueError(f"MCP OAuth {name} URL must use HTTP or HTTPS")


class TestSHA256TokenVerifier(TokenVerifier):
    def __init__(self, records: tuple[TestTokenRecord, ...]) -> None:
        self._records = records

    async def verify_token(self, token: str) -> AccessToken | None:
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        selected: TestTokenRecord | None = None
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


class OAuthJWTTokenVerifier(TokenVerifier):
    """Validate RFC 9068-style OAuth access tokens against an AS JWKS."""

    def __init__(
        self,
        config: MCPRuntimeConfig,
        *,
        jwks_client: Any | None = None,
    ) -> None:
        if config.auth_mode != "oauth":
            raise ValueError("OAuthJWTTokenVerifier requires oauth auth mode")
        self._config = config
        self._jwks_client = jwks_client or jwt.PyJWKClient(
            str(config.oauth_jwks_url),
            cache_keys=True,
            max_cached_keys=32,
            cache_jwk_set=True,
            lifespan=300,
            timeout=config.oauth_jwks_timeout_seconds,
        )

    def _decode(self, token: str) -> dict[str, Any]:
        header = jwt.get_unverified_header(token)
        if self._config.oauth_require_at_jwt and header.get("typ") not in {
            "at+jwt",
            "application/at+jwt",
        }:
            raise jwt.InvalidTokenError("OAuth access token typ is not at+jwt")
        signing_key = self._jwks_client.get_signing_key_from_jwt(token)
        required_claims = ["iss", "aud", "exp", "sub"]
        if self._config.oauth_require_at_jwt:
            required_claims.extend(["client_id", "iat", "jti"])
        return jwt.decode(
            token,
            signing_key.key,
            algorithms=list(self._config.oauth_algorithms),
            audience=self._config.oauth_audience,
            issuer=self._config.issuer_url,
            leeway=self._config.oauth_clock_skew_seconds,
            options={"require": required_claims},
        )

    def _access_token(self, token: str, claims: Mapping[str, Any]) -> AccessToken:
        client_id = str(claims.get("client_id") or claims.get("azp") or "").strip()
        actor_id = str(claims.get("sub") or "").strip()
        tenant_id = (
            actor_id
            if self._config.oauth_tenant_mode == "subject"
            else str(claims.get(self._config.oauth_tenant_claim) or "").strip()
        )
        raw_scopes = claims.get(self._config.oauth_scope_claim)
        if isinstance(raw_scopes, str):
            scopes = [scope for scope in raw_scopes.split() if scope]
        elif isinstance(raw_scopes, list) and all(
            isinstance(scope, str) for scope in raw_scopes
        ):
            scopes = list(raw_scopes)
        else:
            scopes = []
        if self._config.oauth_permissions_claim:
            raw_permissions = claims.get(self._config.oauth_permissions_claim)
            if isinstance(raw_permissions, list) and all(
                isinstance(permission, str) for permission in raw_permissions
            ):
                scopes.extend(raw_permissions)
        scopes = list(dict.fromkeys(scopes))
        if not all(
            PRINCIPAL_IDENTIFIER.fullmatch(value or "")
            for value in (client_id, actor_id, tenant_id)
        ):
            raise jwt.InvalidTokenError(
                "OAuth access token is missing a valid client, subject, or tenant"
            )
        if not scopes or not all(SAFE_IDENTIFIER.fullmatch(scope) for scope in scopes):
            raise jwt.InvalidTokenError("OAuth access token has no valid scopes")
        if (
            self._config.oauth_allowed_tenants
            and tenant_id not in self._config.oauth_allowed_tenants
        ):
            raise jwt.InvalidTokenError("OAuth tenant is not allowed")
        return AccessToken(
            token=token,
            client_id=client_id,
            subject=actor_id,
            scopes=scopes,
            expires_at=int(claims["exp"]),
            resource=self._config.oauth_audience,
            claims={
                "iss": str(claims["iss"]),
                "tenant_id": tenant_id,
                "jti": str(claims.get("jti") or ""),
            },
        )

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            claims = await asyncio.wait_for(
                asyncio.to_thread(self._decode, token),
                timeout=self._config.oauth_jwks_timeout_seconds + 0.5,
            )
            return self._access_token(token, claims)
        except (jwt.PyJWTError, OSError, TimeoutError, ValueError, KeyError):
            return None


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
    "crawler_health": "crawler:read",
    "crawler_list_jobs": "crawler:read",
    "crawler_get_job": "crawler:read",
    "crawler_get_results": "crawler:read",
    "crawler_get_events": "crawler:read",
    "crawler_get_deliveries": "crawler:read",
    "crawler_create_job": "crawler:run",
    "crawler_run_job": "crawler:run",
    "crawler_pause_job": "crawler:control",
    "crawler_resume_job": "crawler:control",
    "crawler_cancel_job": "crawler:cancel",
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
        self.observability = MCPObservability(
            window_seconds=config.metrics_window_seconds,
            max_samples=config.metrics_max_samples,
            alert_min_calls=config.alert_min_calls,
            alert_error_rate=config.alert_error_rate,
            alert_p95_latency_ms=config.alert_p95_latency_ms,
            alert_rate_limit_count=config.alert_rate_limit_count,
            alert_concurrency_rejection_count=(
                config.alert_concurrency_rejection_count
            ),
            alert_webhook_url=config.alert_webhook_url,
            alert_webhook_timeout_seconds=config.alert_webhook_timeout_seconds,
            alert_webhook_max_attempts=config.alert_webhook_max_attempts,
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
        if not PRINCIPAL_IDENTIFIER.fullmatch(
            tenant_id
        ) or not PRINCIPAL_IDENTIFIER.fullmatch(actor_id):
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
        await self.observability.begin(principal.tenant_id)
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
            error_code = error_code or (
                "concurrency_rejected" if exc.code == -32003 else f"mcp_{exc.code}"
            )
            raise
        except Exception:
            error_code = "internal_error"
            raise
        finally:
            latency_ms = int((time.monotonic() - started) * 1000)
            try:
                await asyncio.to_thread(
                    self.store.finish_mcp_audit,
                    audit_id,
                    status=status,
                    latency_ms=latency_ms,
                    error_code=error_code,
                )
            finally:
                # Never leak an in-flight slot when the durable audit sink fails.
                await self.observability.finish(
                    tenant_id=principal.tenant_id,
                    tool_name=tool_name,
                    status=status,
                    error_code=error_code,
                    latency_ms=latency_ms,
                    audit_id=audit_id,
                    request_id=(
                        str(ctx.request_id) if ctx.request_id is not None else None
                    ),
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
