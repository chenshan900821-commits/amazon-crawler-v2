from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Mapping


class SecretText:
    """A runtime-only secret whose normal string representations are always redacted."""

    __slots__ = ("__value",)

    def __init__(self, value: str) -> None:
        if not value:
            raise ValueError("secret value cannot be empty")
        self.__value = value

    def reveal(self) -> str:
        return self.__value

    def __str__(self) -> str:
        return "<redacted>"

    def __repr__(self) -> str:
        return "SecretText(<redacted>)"


class ResourceOutcome(StrEnum):
    SUCCESS = "success"
    BLOCKED = "blocked"
    AUTH_INVALID = "auth_invalid"
    NETWORK_ERROR = "network_error"
    PROXY_ERROR = "proxy_error"
    PARSE_ERROR = "parse_error"


@dataclass(frozen=True, slots=True)
class CookieLease:
    lease_id: str
    marketplace_id: str
    postal_code: str | None
    cookie_header: SecretText = field(repr=False)
    source: str = "unknown"


@dataclass(frozen=True, slots=True)
class ProxyLease:
    lease_id: str
    proxy_url: SecretText = field(repr=False)
    source: str = "unknown"


@dataclass(frozen=True, slots=True)
class FingerprintProfile:
    profile_id: str
    headers: Mapping[str, str]
    impersonate: str | None = None


@dataclass(frozen=True, slots=True)
class RequestContext:
    cookie: CookieLease | None
    proxy: ProxyLease | None
    fingerprint: FingerprintProfile


@dataclass(frozen=True, slots=True)
class ResourceHealth:
    provider: str
    available: int
    quarantined: int
    groups: int
    last_refresh_at: datetime | None
    stale: bool

    def as_public_dict(self) -> dict[str, str | int | bool | None]:
        return {
            "provider": self.provider,
            "available": self.available,
            "quarantined": self.quarantined,
            "groups": self.groups,
            "last_refresh_at": self.last_refresh_at.isoformat() if self.last_refresh_at else None,
            "stale": self.stale,
        }


@dataclass(frozen=True, slots=True)
class HarvestReport:
    marketplace_id: str
    postal_code: str
    requested: int
    created: int
    rejected: int
    available_after: int
    failure_codes: tuple[str, ...] = ()
