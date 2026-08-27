from __future__ import annotations

import json
import hashlib
import zlib
from dataclasses import dataclass, field
from typing import Any, Protocol

from amazon_crawler.application.service import CrawlerService
from amazon_crawler.domain.errors import CrawlerError
from amazon_crawler.plugins.amazon_parser import (
    LEGACY_DIMENSION_FIELDS,
    LEGACY_PRODUCT_FIELDS,
)


LEGACY_TASKS = {
    "search_jp": ("amazon_search_product_jp_task_boost", "amazon_search_product", "search"),
    "search_hour_jp": ("amazon_search_product_task_hour", "amazon_search_product", "search_hour"),
    "product_jp": ("amazon_product_details_jp_task_boost", "amazon_product_details", "product"),
    "product_hw_jp": ("amazon_product_details_jp_task_boost_hw", "amazon_product_details", "product_hw"),
    "merchant": ("amazon_merchant_detail_task_boost", "amazon_merchant_detail", "merchant"),
    "asin_list_jp": ("amazon_category_asin_list_task", "amazon_category_asin_list", "category_asin_list"),
    "rank_list_jp": ("amazon_rank_list_product_jp_task_boost", "amazon_rank_list_product", "rank_list"),
    "product_time_jp": ("amazon_product_details_time_task", "amazon_product_details_time", "product_time"),
    "reviews": ("amazon_reviews_task", "amazon_reviews", "reviews"),
    "merchant_home": ("amazon_merchant_products_origin_task", "amazon_merchant_products_task", "merchant_home"),
    "merchant_products": ("amazon_merchant_products_task", "amazon_merchant_products", "merchant_products"),
}
ALLOWED_TABLES = {
    table
    for task in LEGACY_TASKS.values()
    for table in task[:2]
} | {"amazon_dimensions_detail", "amazon_product_details_jp_task_boost"}
V2_KIND_ALIASES = {
    "amazon.product": "product",
    "product_jp": "product",
    "product_hw_jp": "product_hw",
    "product_time_jp": "product_time",
    "search_jp": "search",
    "search_hour_jp": "search_hour",
    "asin_list_jp": "category_asin_list",
    "rank_list_jp": "rank_list",
}
LEGACY_EXECUTION_MODES = {
    "product_hw_jp": "overseas",
    "product_time_jp": "realtime",
}
LEGACY_MAX_ATTEMPTS = {
    # The old Funboost setting is max_retry_times=10 plus the initial run.
    "search_hour_jp": 11,
}


def assert_legacy_job_compatibility(
    task_name: str,
    job: dict[str, Any],
    *,
    legacy_task_id: Any | None = None,
) -> None:
    """Reject writes when a V2 job does not prove its legacy destination."""

    if task_name not in LEGACY_TASKS:
        raise ValueError("unsupported legacy task")
    expected_kind = LEGACY_TASKS[task_name][2]
    actual_kind = V2_KIND_ALIASES.get(str(job.get("kind") or ""), job.get("kind"))
    if actual_kind != expected_kind:
        raise ValueError(
            f"legacy task {task_name!r} requires a {expected_kind!r} V2 job"
        )
    if legacy_task_id is None:
        return
    items = job.get("items")
    if not isinstance(items, list) or len(items) != 1:
        raise ValueError("legacy state sync requires exactly one imported task item")
    input_value = items[0].get("input") if isinstance(items[0], dict) else None
    imported_id = input_value.get("id") if isinstance(input_value, dict) else None
    if imported_id in {None, ""} or str(imported_id) != str(legacy_task_id):
        raise ValueError("legacy task ID does not match the imported V2 job item")


def legacy_state_for(
    v2_status: str,
    error_code: str | None = None,
    task_name: str | None = None,
) -> int:
    if v2_status == "succeeded":
        return 1
    if v2_status in {"pending", "running", "paused", "pause_requested"}:
        return 0
    if error_code in {
        "product_not_found",
        "no_results",
        "merchant_has_no_products",
        "merchant_page_has_no_products",
    }:
        return -3
    if error_code in {
        "one_page_only",
        "no_reviews",
        "no_category_results",
        "no_rank_results",
    }:
        return -4
    if error_code in {
        "missing_product_identity",
        "unsupported_product_type",
        "cookie_unavailable",
    }:
        return -2
    if v2_status in {"cancelled", "cancel_requested"}:
        return -5
    return -1


class LegacyTaskSource(Protocol):
    def fetch_pending(self, task_name: str, limit: int) -> list[dict[str, Any]]: ...


class LegacyBufferClient(Protocol):
    def rpush(self, key: str, *values: bytes) -> int: ...

    def eval(self, script: str, numkeys: int, *keys_and_args: Any) -> Any: ...


class SQLAlchemyLegacyGateway:
    def __init__(self, url: str) -> None:
        try:
            import sqlalchemy
        except ImportError as exc:
            raise RuntimeError("legacy MySQL support requires SQLAlchemy") from exc
        self._sqlalchemy = sqlalchemy
        self._engine = sqlalchemy.create_engine(
            url,
            pool_pre_ping=True,
            hide_parameters=True,
        )

    def close(self) -> None:
        """Release pooled DBAPI connections owned by this gateway."""

        self._engine.dispose()

    @staticmethod
    def _table(name: str) -> str:
        if name not in ALLOWED_TABLES:
            raise ValueError("legacy table is not allowlisted")
        return name

    @staticmethod
    def _insert_statement(table: Any, dialect_name: str) -> Any:
        statement = table.insert()
        # The old saver and merchant detail-task expansion both use
        # INSERT IGNORE. Preserve that duplicate-safe behavior for MySQL and
        # MariaDB, especially because the compatibility bridge is at-least-once.
        if dialect_name in {"mysql", "mariadb"}:
            statement = statement.prefix_with("IGNORE")
        return statement

    def fetch_pending(self, task_name: str, limit: int) -> list[dict[str, Any]]:
        if task_name not in LEGACY_TASKS:
            raise ValueError("unsupported legacy task")
        table = self._table(LEGACY_TASKS[task_name][0])
        bounded = max(1, min(int(limit), 10_000))
        statement = self._sqlalchemy.text(
            f"SELECT * FROM `{table}` WHERE state = 0 ORDER BY id LIMIT :limit"
        )
        try:
            with self._engine.connect() as connection:
                rows = connection.execute(statement, {"limit": bounded}).mappings().all()
        except Exception as exc:
            raise RuntimeError("legacy task query failed") from exc
        return [dict(row) for row in rows]

    def fetch_by_states(
        self,
        task_name: str,
        states: list[int] | tuple[int, ...],
        *,
        limit: int,
    ) -> list[dict[str, Any]]:
        """Read bounded historical task rows for migration diagnostics only."""

        if task_name not in LEGACY_TASKS:
            raise ValueError("unsupported legacy task")
        normalized_states = sorted({int(state) for state in states})
        if not normalized_states or any(state < -100 or state > 100 for state in normalized_states):
            raise ValueError("legacy states must be a non-empty bounded integer list")
        table = self._table(LEGACY_TASKS[task_name][0])
        bounded = max(1, min(int(limit), 1_000))
        statement = self._sqlalchemy.text(
            f"SELECT * FROM `{table}` WHERE state IN :states "
            "ORDER BY id DESC LIMIT :limit"
        ).bindparams(self._sqlalchemy.bindparam("states", expanding=True))
        try:
            with self._engine.connect() as connection:
                rows = connection.execute(
                    statement,
                    {"states": normalized_states, "limit": bounded},
                ).mappings().all()
        except Exception as exc:
            raise RuntimeError("legacy historical task query failed") from exc
        return [dict(row) for row in rows]

    def insert_rows(self, table_name: str, rows: list[dict[str, Any]]) -> int:
        if not rows:
            return 0
        table_name = self._table(table_name)
        try:
            metadata = self._sqlalchemy.MetaData()
            table = self._sqlalchemy.Table(table_name, metadata, autoload_with=self._engine)
            columns = {column.name for column in table.columns}
            projected = [
                {key: value for key, value in row.items() if key in columns}
                for row in rows
            ]
            if any(not row for row in projected):
                raise ValueError("legacy row has no columns accepted by the target table")
            grouped: dict[tuple[str, ...], list[dict[str, Any]]] = {}
            for row in projected:
                grouped.setdefault(tuple(sorted(row)), []).append(row)
            with self._engine.begin() as connection:
                for group in grouped.values():
                    connection.execute(
                        self._insert_statement(
                            table,
                            self._engine.dialect.name,
                        ),
                        group,
                    )
        except Exception as exc:
            raise RuntimeError("legacy result insert failed") from exc
        return len(rows)

    def update_task_state(self, task_name: str, task_id: Any, state: int) -> None:
        if task_name not in LEGACY_TASKS:
            raise ValueError("unsupported legacy task")
        table = self._table(LEGACY_TASKS[task_name][0])
        statement = self._sqlalchemy.text(
            f"UPDATE `{table}` SET state = :state WHERE id = :task_id"
        )
        try:
            with self._engine.begin() as connection:
                result = connection.execute(
                    statement, {"state": state, "task_id": task_id}
                )
                if result.rowcount != 1:
                    exists = connection.execute(
                        self._sqlalchemy.text(
                            f"SELECT 1 FROM `{table}` WHERE id = :task_id LIMIT 1"
                        ),
                        {"task_id": task_id},
                    ).first()
                    if not exists:
                        raise ValueError("legacy task row does not exist")
        except Exception as exc:
            raise RuntimeError("legacy task state update failed") from exc


@dataclass(frozen=True, slots=True)
class ImportReport:
    task_name: str
    read: int
    created: int
    existing: int
    rejected: int
    job_ids: tuple[str, ...] = field(default_factory=tuple)


class LegacyTaskImporter:
    def __init__(self, source: LegacyTaskSource, service: CrawlerService) -> None:
        self._source = source
        self._service = service

    def import_pending(self, task_name: str, *, limit: int = 100) -> ImportReport:
        if task_name not in LEGACY_TASKS:
            raise ValueError("unsupported legacy task")
        rows = self._source.fetch_pending(task_name, limit)
        v2_kind = LEGACY_TASKS[task_name][2]
        created = 0
        existing = 0
        rejected = 0
        job_ids: list[str] = []
        for row in rows:
            task_id = row.get("id")
            if task_id is None:
                rejected += 1
                continue
            try:
                job, was_created = self._service.create_job(
                    inputs=[row],
                    kind=v2_kind,
                    execution_mode=LEGACY_EXECUTION_MODES.get(
                        task_name,
                        "standard",
                    ),
                    idempotency_key=f"legacy:{task_name}:{task_id}",
                    # All other old workers use max_retry_times=4 plus the
                    # initial run.
                    max_attempts=LEGACY_MAX_ATTEMPTS.get(task_name, 5),
                )
            except (CrawlerError, TypeError, ValueError):
                rejected += 1
                continue
            created += int(was_created)
            existing += int(not was_created)
            job_ids.append(job["id"])
        return ImportReport(
            task_name=task_name,
            read=len(rows),
            created=created,
            existing=existing,
            rejected=rejected,
            job_ids=tuple(job_ids),
        )


@dataclass(frozen=True, slots=True)
class LegacyProjection:
    result_rows: tuple[dict[str, Any], ...]
    dimension_rows: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    child_task_rows: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    product_task_rows: tuple[dict[str, Any], ...] = field(default_factory=tuple)


def project_legacy_result(task_name: str, result: dict[str, Any]) -> LegacyProjection:
    if task_name not in LEGACY_TASKS:
        raise ValueError("unsupported legacy task")
    data = result.get("data") if isinstance(result.get("data"), dict) else result
    if task_name in {"product_jp", "product_hw_jp", "product_time_jp"}:
        product = {field: data.get(field) for field in LEGACY_PRODUCT_FIELDS}
        dimensions = ()
        if task_name != "product_time_jp":
            dimensions = tuple(
                {
                    field: row[field]
                    for field in LEGACY_DIMENSION_FIELDS
                    if field in row
                }
                for row in data.get("dimension_items", [])
                if isinstance(row, dict)
            )
        return LegacyProjection((product,), dimensions)
    if task_name == "merchant":
        item = data.get("item")
        return LegacyProjection((item,) if isinstance(item, dict) else ())
    if task_name == "merchant_home":
        rows = tuple(row for row in data.get("child_tasks", []) if isinstance(row, dict))
        return LegacyProjection((), child_task_rows=rows)
    if task_name == "merchant_products":
        rows = tuple(row for row in data.get("items", []) if isinstance(row, dict))
        tasks = tuple(
            row for row in data.get("product_detail_tasks", []) if isinstance(row, dict)
        )
        return LegacyProjection(rows, product_task_rows=tasks)
    rows = tuple(row for row in data.get("items", []) if isinstance(row, dict))
    return LegacyProjection(rows)


class LegacyRedisResultBuffer:
    _PUBLISH_ONCE_SCRIPT = """
        if redis.call('EXISTS', KEYS[1]) == 1 then
            return {0, 0, 1}
        end
        local result_count = tonumber(ARGV[2])
        local cursor = 3
        for i = 1, result_count do
            redis.call('RPUSH', KEYS[2], ARGV[cursor])
            cursor = cursor + 1
        end
        local dimension_count = tonumber(ARGV[cursor])
        cursor = cursor + 1
        for i = 1, dimension_count do
            redis.call('RPUSH', KEYS[3], ARGV[cursor])
            cursor = cursor + 1
        end
        redis.call('SET', KEYS[1], '1', 'EX', tonumber(ARGV[1]))
        return {result_count, dimension_count, 0}
    """

    def __init__(self, client: LegacyBufferClient) -> None:
        self._client = client

    @staticmethod
    def _encode(row: dict[str, Any]) -> bytes:
        return zlib.compress(
            json.dumps(row, ensure_ascii=False, default=str).encode("utf-8")
        )

    def publish(self, task_name: str, result: dict[str, Any]) -> dict[str, int]:
        projection = project_legacy_result(task_name, result)
        counts = {"result_rows": 0, "dimension_rows": 0}
        result_rows = projection.result_rows or projection.child_task_rows
        if result_rows:
            key = f"amazon_{task_name}_items_buffer"
            values = [self._encode(row) for row in result_rows]
            try:
                self._client.rpush(key, *values)
            except Exception as exc:
                raise RuntimeError("legacy Redis result write failed") from exc
            counts["result_rows"] = len(values)
        if projection.dimension_rows:
            values = [self._encode(row) for row in projection.dimension_rows]
            try:
                self._client.rpush(
                    "amazon_product_jp_dimession_items_buffer", *values
                )
            except Exception as exc:
                raise RuntimeError("legacy Redis dimension write failed") from exc
            counts["dimension_rows"] = len(values)
        return counts

    def publish_once(
        self,
        delivery_id: str,
        task_name: str,
        result: dict[str, Any],
        *,
        receipt_ttl_seconds: int = 90 * 24 * 60 * 60,
    ) -> dict[str, int | bool]:
        """Atomically de-duplicate one outbox delivery and append legacy rows."""

        projection = project_legacy_result(task_name, result)
        result_rows = projection.result_rows or projection.child_task_rows
        result_values = [self._encode(row) for row in result_rows]
        dimension_values = [self._encode(row) for row in projection.dimension_rows]
        marker_hash = hashlib.sha256(delivery_id.encode("utf-8")).hexdigest()
        marker_key = f"amazon_crawler_v2:delivery:{marker_hash}"
        result_key = f"amazon_{task_name}_items_buffer"
        dimension_key = "amazon_product_jp_dimession_items_buffer"
        arguments: list[Any] = [
            max(60, int(receipt_ttl_seconds)),
            len(result_values),
            *result_values,
            len(dimension_values),
            *dimension_values,
        ]
        try:
            reply = self._client.eval(
                self._PUBLISH_ONCE_SCRIPT,
                3,
                marker_key,
                result_key,
                dimension_key,
                *arguments,
            )
        except Exception as exc:
            raise RuntimeError("legacy Redis idempotent result write failed") from exc
        if not isinstance(reply, (list, tuple)) or len(reply) != 3:
            raise RuntimeError("legacy Redis returned an invalid delivery receipt")
        return {
            "result_rows": int(reply[0]),
            "dimension_rows": int(reply[1]),
            "deduplicated": bool(int(reply[2])),
        }


class LegacyMySQLResultWriter:
    def __init__(self, gateway: SQLAlchemyLegacyGateway) -> None:
        self._gateway = gateway

    def publish(self, task_name: str, result: dict[str, Any]) -> dict[str, int]:
        projection = project_legacy_result(task_name, result)
        result_table = LEGACY_TASKS[task_name][1]
        standard_product_tasks = [
            row
            for row in projection.product_task_rows
            if row.get("market_id") != "A1VC38T7YXB528"
        ]
        hardware_product_tasks = [
            row
            for row in projection.product_task_rows
            if row.get("market_id") == "A1VC38T7YXB528"
        ]
        counts = {
            "result_rows": self._gateway.insert_rows(result_table, list(projection.result_rows)),
            "dimension_rows": self._gateway.insert_rows(
                "amazon_dimensions_detail", list(projection.dimension_rows)
            ),
            "child_task_rows": self._gateway.insert_rows(
                "amazon_merchant_products_task", list(projection.child_task_rows)
            ),
            "product_task_rows": self._gateway.insert_rows(
                "amazon_product_details_jp_task_boost", standard_product_tasks
            ),
            "product_hw_task_rows": self._gateway.insert_rows(
                "amazon_product_details_jp_task_boost_hw", hardware_product_tasks
            ),
        }
        return counts
