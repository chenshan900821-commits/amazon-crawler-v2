from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class JobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSE_REQUESTED = "pause_requested"
    PAUSED = "paused"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"


class ItemStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ExecutionMode(StrEnum):
    STANDARD = "standard"
    OVERSEAS = "overseas"
    REALTIME = "realtime"


@dataclass(frozen=True, slots=True)
class NormalizedInput:
    asin: str | None = None
    marketplace_id: str = ""
    source: str | dict[str, Any] = ""
    product_url: str | None = None
    postal_code: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    explicit_input_key: str | None = None

    @property
    def input_key(self) -> str:
        if self.explicit_input_key:
            return self.explicit_input_key
        postal = self.postal_code or "-"
        return f"{self.marketplace_id}:{self.asin}:{postal}"

    def as_dict(self) -> dict[str, Any]:
        if self.payload:
            return dict(self.payload)
        return {
            "asin": self.asin,
            "marketplace_id": self.marketplace_id,
            "source": self.source,
            "product_url": self.product_url,
            "postal_code": self.postal_code,
        }

    @classmethod
    def from_payload(
        cls,
        payload: dict[str, Any],
        *,
        input_key: str,
        marketplace_id: str,
        postal_code: str | None,
        source: str | dict[str, Any],
    ) -> "NormalizedInput":
        return cls(
            marketplace_id=marketplace_id,
            source=source,
            postal_code=postal_code,
            payload=payload,
            explicit_input_key=input_key,
        )


@dataclass(frozen=True, slots=True)
class ClaimedItem:
    id: str
    job_id: str
    seq: int
    kind: str
    execution_mode: ExecutionMode
    input: dict[str, Any]
    options: dict[str, Any]
    attempts: int
    max_attempts: int
    lease_owner: str
    lease_token: str


@dataclass(frozen=True, slots=True)
class FollowupJob:
    """A durable job that must be created atomically with a parent result."""

    kind: str
    inputs: tuple[NormalizedInput, ...]
    execution_mode: str = "standard"
    priority: int = 0
    max_attempts: int = 5
    reason: str = "plugin_followup"


@dataclass(frozen=True, slots=True)
class CrawlResult:
    data: dict[str, Any]
    evidence: dict[str, Any] = field(default_factory=dict)
    schema_version: str = "amazon.product.v1"
    followup_jobs: tuple[FollowupJob, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class CrawlFailure:
    code: str
    message: str
    retryable: bool
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PluginOutcome:
    result: CrawlResult | None = None
    failure: CrawlFailure | None = None

    @property
    def ok(self) -> bool:
        return self.result is not None and self.failure is None


@dataclass(frozen=True, slots=True)
class ClaimedDelivery:
    """One durable result-outbox delivery claimed by a sink worker."""

    id: str
    result_id: str
    job_id: str
    item_id: str
    sink_name: str
    kind: str
    schema_version: str
    data: dict[str, Any]
    evidence: dict[str, Any]
    collected_at: str
    attempts: int
    max_attempts: int
    lease_owner: str
    lease_token: str
