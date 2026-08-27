from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from pathlib import Path
from typing import Any, Iterable

from amazon_crawler.domain.models import ClaimedDelivery
from amazon_crawler.domain.ports import ResultSink
from amazon_crawler.infra.legacy_compat import (
    LegacyMySQLResultWriter,
    LegacyRedisResultBuffer,
)


LEGACY_TASK_BY_KIND = {
    "amazon.product": "product_jp",
    "product": "product_jp",
    "product_jp": "product_jp",
    "product_hw": "product_hw_jp",
    "product_hw_jp": "product_hw_jp",
    "product_time": "product_time_jp",
    "product_time_jp": "product_time_jp",
    "search": "search_jp",
    "search_jp": "search_jp",
    "search_hour": "search_hour_jp",
    "search_hour_jp": "search_hour_jp",
    "reviews": "reviews",
    "merchant": "merchant",
    "merchant_home": "merchant_home",
    "merchant_products": "merchant_products",
    "category_asin_list": "asin_list_jp",
    "asin_list_jp": "asin_list_jp",
    "rank_list": "rank_list_jp",
    "rank_list_jp": "rank_list_jp",
}


def legacy_task_for_kind(kind: str) -> str:
    try:
        return LEGACY_TASK_BY_KIND[kind]
    except KeyError as exc:
        raise ValueError("result kind has no legacy storage projection") from exc


class ResultSinkRegistry:
    """Runtime-only sink registry; connection details never enter job payloads."""

    def __init__(self, sinks: Iterable[ResultSink] = ()) -> None:
        self._sinks: dict[str, ResultSink] = {"sqlite": _CanonicalSQLiteSink()}
        for sink in sinks:
            if sink.name == "sqlite" or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", sink.name):
                raise ValueError("invalid or reserved result sink name")
            if sink.name in self._sinks:
                raise ValueError(f"duplicate result sink: {sink.name}")
            self._sinks[sink.name] = sink

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._sinks))

    def get(self, name: str) -> ResultSink:
        try:
            return self._sinks[name]
        except KeyError as exc:
            raise ValueError("result sink is not configured") from exc

    def public_capabilities(self) -> list[dict[str, Any]]:
        values = []
        for name in self.names:
            sink = self._sinks[name]
            values.append(
                {
                    "name": name,
                    "canonical": name == "sqlite",
                    "delivery": "transactional" if name == "sqlite" else "outbox",
                    "description": getattr(sink, "description", "configured result sink"),
                }
            )
        return values


class _CanonicalSQLiteSink:
    name = "sqlite"
    description = "canonical task, checkpoint and result record"

    def publish(self, delivery: ClaimedDelivery) -> dict[str, Any]:
        raise RuntimeError("the canonical SQLite sink is committed before the outbox")


class JsonlResultSink:
    """Append-only JSONL sink with a stable delivery ID and retry de-duplication."""

    name = "jsonl"
    description = "append-only JSON Lines export for downstream processing"

    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().absolute()
        self._thread_lock = threading.Lock()

    @staticmethod
    def _safe_kind(kind: str) -> str:
        return re.sub(r"[^a-zA-Z0-9_.-]+", "_", kind)[:80] or "unknown"

    @staticmethod
    def _marker_name(delivery_id: str) -> str:
        return hashlib.sha256(delivery_id.encode("utf-8")).hexdigest() + ".done"

    def _record(self, delivery: ClaimedDelivery) -> dict[str, Any]:
        return {
            "delivery_id": delivery.id,
            "result_id": delivery.result_id,
            "job_id": delivery.job_id,
            "item_id": delivery.item_id,
            "kind": delivery.kind,
            "schema_version": delivery.schema_version,
            "data": delivery.data,
            "evidence": delivery.evidence,
            "collected_at": delivery.collected_at,
        }

    def publish(self, delivery: ClaimedDelivery) -> dict[str, Any]:
        try:
            import fcntl
        except ImportError as exc:  # pragma: no cover - supported deployment is POSIX
            raise RuntimeError("JSONL sink requires POSIX file locking") from exc

        if self.root.is_symlink():
            raise RuntimeError("JSONL result root must not be a symbolic link")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.root.is_symlink() or not self.root.is_dir():
            raise RuntimeError("JSONL result root is not a safe directory")
        self.root.chmod(0o700)
        receipts = self.root / ".receipts"
        if receipts.is_symlink():
            raise RuntimeError("JSONL receipt directory must not be a symbolic link")
        receipts.mkdir(mode=0o700, exist_ok=True)
        if receipts.is_symlink() or not receipts.is_dir():
            raise RuntimeError("JSONL receipt directory is not safe")
        receipts.chmod(0o700)
        marker = receipts / self._marker_name(delivery.id)
        destination = self.root / f"{self._safe_kind(delivery.kind)}.jsonl"
        if marker.is_symlink() or destination.is_symlink():
            raise RuntimeError("JSONL result artifacts must not be symbolic links")
        needle = (
            '"delivery_id":'
            + json.dumps(delivery.id, ensure_ascii=False, separators=(",", ":"))
        ).encode("utf-8")
        record = json.dumps(
            self._record(delivery),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8") + b"\n"

        deduplicated = False
        flags = (
            os.O_RDWR
            | os.O_APPEND
            | os.O_CREAT
            | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor = os.open(destination, flags, 0o600)
        os.fchmod(descriptor, 0o600)
        with self._thread_lock, os.fdopen(descriptor, "a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                if marker.exists():
                    deduplicated = True
                else:
                    handle.seek(0)
                    if needle in handle.read():
                        deduplicated = True
                    else:
                        handle.seek(0, os.SEEK_END)
                        handle.write(record)
                        handle.flush()
                        os.fsync(handle.fileno())
                    marker_flags = (
                        os.O_WRONLY
                        | os.O_CREAT
                        | os.O_TRUNC
                        | getattr(os, "O_NOFOLLOW", 0)
                    )
                    try:
                        marker_descriptor = os.open(marker, marker_flags, 0o600)
                        try:
                            os.fchmod(marker_descriptor, 0o600)
                            os.write(marker_descriptor, delivery.result_id.encode("utf-8"))
                            os.fsync(marker_descriptor)
                        finally:
                            os.close(marker_descriptor)
                    except OSError as exc:
                        raise RuntimeError("JSONL delivery receipt write failed") from exc
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return {
            "sink": self.name,
            "delivery_id": delivery.id,
            "file": destination.name,
            "deduplicated": deduplicated,
        }


class LegacyRedisResultSink:
    name = "legacy_redis"
    description = "legacy compressed Redis result buffers"

    def __init__(self, writer: LegacyRedisResultBuffer) -> None:
        self.writer = writer

    def publish(self, delivery: ClaimedDelivery) -> dict[str, Any]:
        task_name = legacy_task_for_kind(delivery.kind)
        counts = self.writer.publish_once(
            delivery.id,
            task_name,
            {"data": delivery.data},
        )
        return {
            "sink": self.name,
            "delivery_id": delivery.id,
            "legacy_task": task_name,
            **counts,
        }


class LegacyMySQLResultSink:
    name = "legacy_mysql"
    description = "legacy MySQL tables through allowlisted projections"

    def __init__(self, writer: LegacyMySQLResultWriter) -> None:
        self.writer = writer

    def publish(self, delivery: ClaimedDelivery) -> dict[str, Any]:
        task_name = legacy_task_for_kind(delivery.kind)
        counts = self.writer.publish(task_name, {"data": delivery.data})
        return {
            "sink": self.name,
            "delivery_id": delivery.id,
            "legacy_task": task_name,
            "delivery_semantics": "at_least_once_with_legacy_insert_ignore",
            **counts,
        }
