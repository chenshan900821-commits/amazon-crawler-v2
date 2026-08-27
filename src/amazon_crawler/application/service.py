from __future__ import annotations

import uuid
from typing import Any

from amazon_crawler.domain.errors import ValidationError
from amazon_crawler.domain.models import ExecutionMode
from amazon_crawler.domain.ports import StateStore
from amazon_crawler.infra.sqlite_store import stable_idempotency_key
from amazon_crawler.plugins.marketplaces import public_marketplaces
from amazon_crawler.plugins.registry import PluginRegistry


LEGACY_DEFAULT_MAX_ATTEMPTS = 5
LEGACY_HOURLY_MAX_ATTEMPTS = 11


class CrawlerService:
    def __init__(
        self,
        store: StateStore,
        plugins: PluginRegistry,
        *,
        result_sinks: tuple[str, ...] | list[str] | set[str] = ("sqlite",),
    ) -> None:
        self.store = store
        self.plugins = plugins
        self.result_sinks = frozenset({"sqlite", *result_sinks})

    def create_job(
        self,
        *,
        inputs: list[Any],
        kind: str = "amazon.product",
        marketplace_id: str | None = None,
        postal_code: str | None = None,
        execution_mode: str = "standard",
        priority: int = 0,
        max_attempts: int | None = None,
        idempotency_key: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], bool]:
        if not inputs:
            raise ValidationError("at least one task input is required")
        if len(inputs) > 500:
            raise ValidationError("a job may contain at most 500 inputs")
        try:
            mode = ExecutionMode(execution_mode)
        except ValueError as exc:
            raise ValidationError("execution_mode must be standard, overseas, or realtime") from exc
        if not -100 <= priority <= 100:
            raise ValidationError("priority must be between -100 and 100")
        plugin = self.plugins.get(kind)
        base_kind = getattr(plugin, "base_kind", plugin.kind)
        effective_max_attempts = (
            LEGACY_HOURLY_MAX_ATTEMPTS
            if max_attempts is None and base_kind == "search_hour"
            else LEGACY_DEFAULT_MAX_ATTEMPTS
            if max_attempts is None
            else max_attempts
        )
        if not 1 <= effective_max_attempts <= 20:
            raise ValidationError("max_attempts must be between 1 and 20")

        normalized = [plugin.normalize(value, marketplace_id, postal_code) for value in inputs]
        deduplicated = list({item.input_key: item for item in normalized}.values())
        safe_options = self._safe_options(options or {})
        effective_priority = 50 if mode is ExecutionMode.REALTIME and priority == 0 else priority
        key_payload = {
            "kind": kind,
            "execution_mode": mode.value,
            "inputs": [item.as_dict() for item in deduplicated],
            "priority": effective_priority,
            "max_attempts": effective_max_attempts,
            "options": safe_options,
        }
        key = idempotency_key.strip() if idempotency_key and idempotency_key.strip() else None
        if key and len(key) > 200:
            raise ValidationError("idempotency_key must be 200 characters or fewer")
        # A product-time request without an explicit observation timestamp means
        # "observe now". Reusing the ordinary stable key would return an older
        # observation forever. Callers that need retry idempotency can supply an
        # idempotency key; legacy rows already carry add_date and their importer
        # supplies a stable legacy key.
        if (
            getattr(plugin, "base_kind", None) == "product_time"
            and not key
            and not any(item.as_dict().get("add_date") for item in deduplicated)
        ):
            key_payload["observation_request_id"] = uuid.uuid4().hex
        return self.store.create_job(
            kind=kind,
            execution_mode=mode.value,
            priority=effective_priority,
            inputs=deduplicated,
            options=safe_options,
            idempotency_key=key or stable_idempotency_key(key_payload),
            max_attempts=effective_max_attempts,
        )

    def _safe_options(self, options: dict[str, Any]) -> dict[str, Any]:
        allowed = {"tags", "requested_fields", "result_sinks"}
        unexpected = sorted(set(options) - allowed)
        if unexpected:
            raise ValidationError("unsupported task options")
        if "tags" in options:
            tags = options["tags"]
            if not isinstance(tags, list) or len(tags) > 20 or not all(
                isinstance(tag, str) and len(tag) <= 50 for tag in tags
            ):
                raise ValidationError("tags must be a list of at most 20 short strings")
        if "requested_fields" in options:
            fields = options["requested_fields"]
            if not isinstance(fields, list) or len(fields) > 50 or not all(
                isinstance(field, str) and len(field) <= 80 for field in fields
            ):
                raise ValidationError("requested_fields must be a list of short strings")
        selected = options.get("result_sinks", ["sqlite"])
        if (
            not isinstance(selected, list)
            or not selected
            or len(selected) > 8
            or not all(
                isinstance(name, str) and 1 <= len(name) <= 64 for name in selected
            )
        ):
            raise ValidationError("result_sinks must be a non-empty list of sink names")
        unknown = sorted(set(selected) - self.result_sinks)
        if unknown:
            raise ValidationError("one or more result sinks are not configured")
        normalized = dict(options)
        normalized["result_sinks"] = sorted({"sqlite", *selected})
        return normalized

    def capabilities(self) -> dict[str, Any]:
        return {
            "api_version": "v1",
            "crawler_version": "0.1.0",
            "plugins": self.plugins.capabilities(),
            "marketplaces": public_marketplaces(),
            "control": ["pause", "resume", "cancel"],
            "agent_safe_operations": [
                "capabilities",
                "create",
                "list",
                "show",
                "pause",
                "resume",
                "cancel_with_confirmation",
                "results",
                "events",
                "deliveries",
                "metrics",
            ],
            "checkpoint_model": "durable-item-state-plus-contiguous-sequence",
            "retry_defaults": {
                "ordinary_total_attempts": LEGACY_DEFAULT_MAX_ATTEMPTS,
                "search_hour_total_attempts": LEGACY_HOURLY_MAX_ATTEMPTS,
                "explicit_override_allowed": True,
            },
            "result_storage": {
                "configured_sinks": sorted(self.result_sinks),
                "canonical_sink": "sqlite",
                "secondary_delivery": "durable-outbox",
                "selection_option": "result_sinks",
            },
        }
