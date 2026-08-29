from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any
from urllib.parse import urlsplit

import httpx

from amazon_crawler.interfaces.mcp_policy import MCPRuntimeConfig


def _https_url(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urlsplit(value)
    return parsed.scheme == "https" and bool(parsed.hostname) and not parsed.fragment


def _check(name: str, ok: bool, detail: str) -> dict[str, Any]:
    return {"name": name, "ok": ok, "detail": detail}


async def _authorization_metadata(
    client: httpx.AsyncClient,
    issuer: str,
) -> tuple[str, dict[str, Any]]:
    base = issuer.rstrip("/")
    candidates = (
        f"{base}/.well-known/oauth-authorization-server",
        f"{base}/.well-known/openid-configuration",
    )
    errors: list[str] = []
    for url in candidates:
        try:
            response = await client.get(url, headers={"Accept": "application/json"})
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                errors.append(f"{url}: response is not a JSON object")
                continue
            return url, payload
        except (httpx.HTTPError, json.JSONDecodeError, ValueError) as exc:
            errors.append(f"{url}: {exc.__class__.__name__}")
    raise ValueError(
        "Auth0 discovery failed at both standard metadata endpoints ("
        + "; ".join(errors)
        + ")"
    )


async def check_auth0_configuration(
    config: MCPRuntimeConfig,
    *,
    client: httpx.AsyncClient | None = None,
    timeout_seconds: float = 8.0,
) -> dict[str, Any]:
    """Validate public Auth0 metadata and JWKS without handling any secret."""
    if config.auth_mode != "oauth" or config.oauth_provider != "auth0":
        raise ValueError(
            "Auth0 preflight requires CRAWLER_MCP_AUTH_MODE=oauth and "
            "CRAWLER_MCP_OAUTH_PROVIDER=auth0"
        )

    owns_client = client is None
    active_client = client or httpx.AsyncClient(
        timeout=httpx.Timeout(timeout_seconds),
        follow_redirects=False,
    )
    try:
        metadata_url, metadata = await _authorization_metadata(
            active_client, str(config.issuer_url)
        )
        jwks_response = await active_client.get(
            str(config.oauth_jwks_url), headers={"Accept": "application/json"}
        )
        jwks_response.raise_for_status()
        jwks = jwks_response.json()
    finally:
        if owns_client:
            await active_client.aclose()

    if not isinstance(jwks, dict):
        raise TypeError("Auth0 JWKS response is not a JSON object")
    keys = jwks.get("keys")
    usable_keys = (
        [
            key
            for key in keys
            if isinstance(key, dict)
            and key.get("kty") == "RSA"
            and key.get("use") in {None, "sig"}
            and key.get("alg") in {None, "RS256"}
            and isinstance(key.get("kid"), str)
            and bool(key["kid"])
        ]
        if isinstance(keys, list)
        else []
    )
    checks = [
        _check(
            "issuer",
            metadata.get("issuer") == config.issuer_url,
            "discovery issuer exactly matches the configured issuer",
        ),
        _check(
            "jwks_uri",
            metadata.get("jwks_uri") == config.oauth_jwks_url,
            "discovery JWKS URI exactly matches the configured JWKS URI",
        ),
        _check(
            "authorization_endpoint",
            _https_url(metadata.get("authorization_endpoint")),
            "authorization endpoint is present and HTTPS",
        ),
        _check(
            "token_endpoint",
            _https_url(metadata.get("token_endpoint")),
            "token endpoint is present and HTTPS",
        ),
        _check(
            "pkce_s256",
            "S256" in (metadata.get("code_challenge_methods_supported") or []),
            "authorization server advertises PKCE S256",
        ),
        _check(
            "rs256_signing_key",
            bool(usable_keys),
            "JWKS contains at least one keyed RSA signing key usable with RS256",
        ),
    ]
    return {
        "ok": all(item["ok"] for item in checks),
        "provider": "auth0",
        "issuer": config.issuer_url,
        "audience": config.oauth_audience,
        "metadata_url": metadata_url,
        "checks": checks,
        "manual_dashboard_checks": [
            "Resource Parameter Compatibility Profile is enabled",
            "Client ID Metadata Document Registration is enabled when public MCP clients need dynamic registration",
            "the MCP API uses token_dialect=rfc9068_profile_authz and RS256",
            "RBAC permissions are enabled and roles grant only required crawler scopes",
            "the selected database or social connection is available to third-party MCP clients",
        ],
        "note": (
            "manual dashboard checks cannot be proven from public discovery metadata; "
            "run an end-to-end login and MCP smoke test before production activation"
        ),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="amazon-crawler-mcp-auth0-check",
        description="Check the public Auth0 configuration used by the MCP server.",
    )
    parser.add_argument("--timeout", type=float, default=8.0)
    return parser


def main() -> None:
    args = _parser().parse_args()
    try:
        config = MCPRuntimeConfig.from_env()
        payload = asyncio.run(
            check_auth0_configuration(config, timeout_seconds=max(0.5, args.timeout))
        )
    except Exception as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": {
                        "type": exc.__class__.__name__,
                        "message": str(exc),
                    },
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        raise SystemExit(2) from exc
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    if not payload["ok"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
