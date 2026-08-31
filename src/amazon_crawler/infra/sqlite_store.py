from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from amazon_crawler.domain.errors import ConflictError, NotFoundError
from amazon_crawler.domain.models import (
    ClaimedDelivery,
    ClaimedItem,
    CrawlResult,
    ExecutionMode,
    FollowupJob,
    NormalizedInput,
)


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso(value: datetime | None = None) -> str:
    return (value or utc_now()).isoformat()


class SQLiteStore:
    """SQLite-backed job state with atomic item leases and durable checkpoints."""

    def __init__(self, path: Path, *, delivery_max_attempts: int = 8) -> None:
        self.path = path
        self.delivery_max_attempts = max(1, min(int(delivery_max_attempts), 100))
        self._schema_lock = threading.Lock()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        try:
            yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        with self._schema_lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as connection:
                connection.execute("PRAGMA journal_mode = WAL")
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS jobs (
                        id TEXT PRIMARY KEY,
                        idempotency_key TEXT NOT NULL UNIQUE,
                        tenant_id TEXT NOT NULL DEFAULT 'local',
                        created_by TEXT NOT NULL DEFAULT 'local',
                        kind TEXT NOT NULL,
                        execution_mode TEXT NOT NULL,
                        status TEXT NOT NULL,
                        priority INTEGER NOT NULL DEFAULT 0,
                        options_json TEXT NOT NULL DEFAULT '{}',
                        total_items INTEGER NOT NULL,
                        succeeded_items INTEGER NOT NULL DEFAULT 0,
                        failed_items INTEGER NOT NULL DEFAULT 0,
                        cancelled_items INTEGER NOT NULL DEFAULT 0,
                        checkpoint_seq INTEGER NOT NULL DEFAULT 0,
                        checkpoint_json TEXT NOT NULL DEFAULT '{}',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        started_at TEXT,
                        finished_at TEXT,
                        last_error_code TEXT,
                        last_error TEXT
                    );

                    CREATE TABLE IF NOT EXISTS job_items (
                        id TEXT PRIMARY KEY,
                        job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                        seq INTEGER NOT NULL,
                        input_key TEXT NOT NULL,
                        input_json TEXT NOT NULL,
                        status TEXT NOT NULL,
                        attempts INTEGER NOT NULL DEFAULT 0,
                        max_attempts INTEGER NOT NULL DEFAULT 5,
                        available_at TEXT NOT NULL,
                        lease_owner TEXT,
                        lease_token TEXT,
                        lease_expires_at TEXT,
                        started_at TEXT,
                        finished_at TEXT,
                        last_error_code TEXT,
                        last_error TEXT,
                        result_id TEXT,
                        UNIQUE(job_id, input_key),
                        UNIQUE(job_id, seq)
                    );

                    CREATE TABLE IF NOT EXISTS results (
                        id TEXT PRIMARY KEY,
                        job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                        item_id TEXT NOT NULL REFERENCES job_items(id) ON DELETE CASCADE UNIQUE,
                        input_key TEXT NOT NULL,
                        schema_version TEXT NOT NULL,
                        data_json TEXT NOT NULL,
                        evidence_json TEXT NOT NULL,
                        collected_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS job_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                        item_id TEXT,
                        event_type TEXT NOT NULL,
                        payload_json TEXT NOT NULL DEFAULT '{}',
                        created_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS result_outbox (
                        id TEXT PRIMARY KEY,
                        result_id TEXT NOT NULL REFERENCES results(id) ON DELETE CASCADE,
                        job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                        item_id TEXT NOT NULL REFERENCES job_items(id) ON DELETE CASCADE,
                        sink_name TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'pending',
                        attempts INTEGER NOT NULL DEFAULT 0,
                        max_attempts INTEGER NOT NULL DEFAULT 8,
                        available_at TEXT NOT NULL,
                        lease_owner TEXT,
                        lease_token TEXT,
                        lease_expires_at TEXT,
                        delivered_at TEXT,
                        last_error TEXT,
                        receipt_json TEXT NOT NULL DEFAULT '{}',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(result_id, sink_name)
                    );

                    CREATE TABLE IF NOT EXISTS mcp_audit_events (
                        id TEXT PRIMARY KEY,
                        tenant_id TEXT NOT NULL,
                        actor_id TEXT NOT NULL,
                        client_id TEXT NOT NULL,
                        tool_name TEXT NOT NULL,
                        arguments_sha256 TEXT NOT NULL,
                        request_id TEXT,
                        status TEXT NOT NULL,
                        error_code TEXT,
                        latency_ms INTEGER,
                        created_at TEXT NOT NULL,
                        finished_at TEXT
                    );

                    CREATE TABLE IF NOT EXISTS mcp_approval_consumptions (
                        nonce TEXT PRIMARY KEY,
                        tenant_id TEXT NOT NULL,
                        actor_id TEXT NOT NULL,
                        action TEXT NOT NULL,
                        parameters_sha256 TEXT NOT NULL,
                        expires_at TEXT NOT NULL,
                        consumed_at TEXT NOT NULL
                    );

                    CREATE INDEX IF NOT EXISTS idx_items_claim
                    ON job_items(status, available_at, job_id, seq);
                    CREATE INDEX IF NOT EXISTS idx_events_job
                    ON job_events(job_id, id DESC);
                    CREATE INDEX IF NOT EXISTS idx_jobs_status
                    ON jobs(status, priority DESC, created_at);
                    CREATE INDEX IF NOT EXISTS idx_result_outbox_claim
                    ON result_outbox(status, available_at, created_at);
                    CREATE INDEX IF NOT EXISTS idx_result_outbox_job
                    ON result_outbox(job_id, created_at);
                    CREATE INDEX IF NOT EXISTS idx_mcp_audit_tenant
                    ON mcp_audit_events(tenant_id, created_at DESC);
                    """
                )
                self._ensure_column(
                    connection,
                    "jobs",
                    "tenant_id",
                    "TEXT NOT NULL DEFAULT 'local'",
                )
                self._ensure_column(
                    connection,
                    "jobs",
                    "created_by",
                    "TEXT NOT NULL DEFAULT 'local'",
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS idx_jobs_tenant "
                    "ON jobs(tenant_id, created_at DESC)"
                )
                self._ensure_column(connection, "job_items", "lease_token", "TEXT")
                self._ensure_column(connection, "result_outbox", "lease_token", "TEXT")
                connection.execute(
                    """
                    UPDATE job_items
                    SET status = 'pending', lease_owner = NULL, lease_token = NULL,
                        lease_expires_at = NULL, available_at = ?,
                        last_error_code = 'lease_schema_upgraded',
                        last_error = 'running lease recovered during token migration'
                    WHERE status = 'running' AND lease_token IS NULL
                    """,
                    (iso(),),
                )
                connection.execute(
                    """
                    UPDATE result_outbox
                    SET status = 'pending', lease_owner = NULL, lease_token = NULL,
                        lease_expires_at = NULL, available_at = ?,
                        last_error = 'delivery lease recovered during token migration',
                        updated_at = ?
                    WHERE status = 'running' AND lease_token IS NULL
                    """,
                    (iso(), iso()),
                )

    @staticmethod
    def _ensure_column(
        connection: sqlite3.Connection,
        table: str,
        column: str,
        declaration: str,
    ) -> None:
        columns = {
            str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")
        }
        if column not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

    def _event(
        self,
        connection: sqlite3.Connection,
        job_id: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
        item_id: str | None = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO job_events(job_id, item_id, event_type, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                job_id,
                item_id,
                event_type,
                json.dumps(payload or {}, ensure_ascii=False),
                iso(),
            ),
        )

    def create_job(
        self,
        *,
        kind: str,
        execution_mode: str,
        priority: int,
        inputs: list[NormalizedInput],
        options: dict[str, Any],
        idempotency_key: str,
        max_attempts: int,
        tenant_id: str = "local",
        created_by: str = "local",
    ) -> tuple[dict[str, Any], bool]:
        now = iso()
        job_id = f"job_{uuid.uuid4().hex}"
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT id FROM jobs WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
            if existing:
                connection.commit()
                return self.get_job(
                    str(existing["id"]),
                    tenant_id=tenant_id,
                ), False
            connection.execute(
                """
                INSERT INTO jobs(
                    id, idempotency_key, tenant_id, created_by,
                    kind, execution_mode, status, priority,
                    options_json, total_items, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    idempotency_key,
                    tenant_id,
                    created_by,
                    kind,
                    execution_mode,
                    priority,
                    json.dumps(options, ensure_ascii=False, sort_keys=True),
                    len(inputs),
                    now,
                    now,
                ),
            )
            for seq, normalized in enumerate(inputs, start=1):
                connection.execute(
                    """
                    INSERT INTO job_items(
                        id, job_id, seq, input_key, input_json, status, max_attempts, available_at
                    ) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
                    """,
                    (
                        f"item_{uuid.uuid4().hex}",
                        job_id,
                        seq,
                        normalized.input_key,
                        json.dumps(
                            normalized.as_dict(), ensure_ascii=False, sort_keys=True
                        ),
                        max_attempts,
                        now,
                    ),
                )
            self._event(connection, job_id, "job.created", {"items": len(inputs)})
            connection.commit()
        return self.get_job(job_id, tenant_id=tenant_id), True

    def _job_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        value["options"] = json.loads(value.pop("options_json"))
        value["checkpoint"] = json.loads(value.pop("checkpoint_json"))
        total = int(value["total_items"])
        terminal = (
            int(value["succeeded_items"])
            + int(value["failed_items"])
            + int(value["cancelled_items"])
        )
        value["progress"] = {
            "terminal_items": terminal,
            "total_items": total,
            "percent": round((terminal / total) * 100, 1) if total else 100.0,
        }
        return value

    def list_jobs(
        self,
        *,
        limit: int = 50,
        status: str | None = None,
        tenant_id: str | None = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM jobs"
        params: list[Any] = []
        conditions: list[str] = []
        if tenant_id is not None:
            conditions.append("tenant_id = ?")
            params.append(tenant_id)
        if status:
            conditions.append("status = ?")
            params.append(status)
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.append(max(1, min(limit, 500)))
        params.append(max(0, int(offset)))
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._job_dict(row) for row in rows]

    def get_job(self, job_id: str, *, tenant_id: str | None = None) -> dict[str, Any]:
        with self._connect() as connection:
            query = "SELECT * FROM jobs WHERE id = ?"
            params: list[Any] = [job_id]
            if tenant_id is not None:
                query += " AND tenant_id = ?"
                params.append(tenant_id)
            row = connection.execute(query, params).fetchone()
            if not row:
                raise NotFoundError(f"job not found: {job_id}")
            items = connection.execute(
                """
                SELECT id, seq, input_key, input_json, status, attempts, max_attempts,
                       available_at, started_at, finished_at, last_error_code, last_error
                FROM job_items WHERE job_id = ? ORDER BY seq
                """,
                (job_id,),
            ).fetchall()
        value = self._job_dict(row)
        value["items"] = [
            {**dict(item), "input": json.loads(item["input_json"])} for item in items
        ]
        for item in value["items"]:
            item.pop("input_json", None)
        return value

    def list_results(
        self,
        job_id: str,
        *,
        limit: int = 100,
        tenant_id: str | None = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        with self._connect() as connection:
            job_query = "SELECT 1 FROM jobs WHERE id = ?"
            job_params: list[Any] = [job_id]
            if tenant_id is not None:
                job_query += " AND tenant_id = ?"
                job_params.append(tenant_id)
            if not connection.execute(job_query, job_params).fetchone():
                raise NotFoundError(f"job not found: {job_id}")
            rows = connection.execute(
                """
                SELECT id, item_id, input_key, schema_version, data_json, evidence_json, collected_at
                FROM results WHERE job_id = ? ORDER BY collected_at, id LIMIT ? OFFSET ?
                """,
                (
                    job_id,
                    max(1, min(limit, 1000)),
                    max(0, int(offset)),
                ),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "item_id": row["item_id"],
                "input_key": row["input_key"],
                "schema_version": row["schema_version"],
                "data": json.loads(row["data_json"]),
                "evidence": json.loads(row["evidence_json"]),
                "collected_at": row["collected_at"],
            }
            for row in rows
        ]

    def list_events(
        self,
        job_id: str,
        *,
        limit: int = 100,
        tenant_id: str | None = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        with self._connect() as connection:
            job_query = "SELECT 1 FROM jobs WHERE id = ?"
            job_params: list[Any] = [job_id]
            if tenant_id is not None:
                job_query += " AND tenant_id = ?"
                job_params.append(tenant_id)
            if not connection.execute(job_query, job_params).fetchone():
                raise NotFoundError(f"job not found: {job_id}")
            rows = connection.execute(
                """
                SELECT id, item_id, event_type, payload_json, created_at
                FROM job_events WHERE job_id = ? ORDER BY id DESC LIMIT ? OFFSET ?
                """,
                (
                    job_id,
                    max(1, min(limit, 500)),
                    max(0, int(offset)),
                ),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "item_id": row["item_id"],
                "event_type": row["event_type"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def _recover_expired(self, connection: sqlite3.Connection) -> int:
        now = iso()
        rows = connection.execute(
            """
            SELECT id, job_id, lease_owner FROM job_items
            WHERE status = 'running' AND lease_expires_at <= ?
            """,
            (now,),
        ).fetchall()
        for row in rows:
            connection.execute(
                """
                UPDATE job_items
                SET status = 'pending', lease_owner = NULL, lease_token = NULL,
                    lease_expires_at = NULL,
                    available_at = ?, last_error_code = 'lease_expired',
                    last_error = 'worker lease expired before completion'
                WHERE id = ? AND status = 'running'
                """,
                (now, row["id"]),
            )
            self._event(
                connection,
                row["job_id"],
                "item.lease_expired",
                {"previous_worker": row["lease_owner"]},
                row["id"],
            )
        return len(rows)

    def recover_expired_leases(self) -> int:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            recovered = self._recover_expired(connection)
            connection.commit()
        return recovered

    def _reconcile_controls(self, connection: sqlite3.Connection) -> None:
        pause_jobs = connection.execute(
            "SELECT id FROM jobs WHERE status = 'pause_requested'"
        ).fetchall()
        for row in pause_jobs:
            running = connection.execute(
                "SELECT COUNT(*) FROM job_items WHERE job_id = ? AND status = 'running'",
                (row["id"],),
            ).fetchone()[0]
            if running == 0:
                connection.execute(
                    "UPDATE jobs SET status = 'paused', updated_at = ? WHERE id = ?",
                    (iso(), row["id"]),
                )
                self._event(connection, row["id"], "job.paused")

        cancel_jobs = connection.execute(
            "SELECT id FROM jobs WHERE status = 'cancel_requested'"
        ).fetchall()
        for row in cancel_jobs:
            now = iso()
            connection.execute(
                """
                UPDATE job_items SET status = 'cancelled', finished_at = ?
                WHERE job_id = ? AND status = 'pending'
                """,
                (now, row["id"]),
            )
            running = connection.execute(
                "SELECT COUNT(*) FROM job_items WHERE job_id = ? AND status = 'running'",
                (row["id"],),
            ).fetchone()[0]
            if running == 0:
                self._refresh_job(connection, row["id"], force_status="cancelled")
                self._event(connection, row["id"], "job.cancelled")

    def claim_next(
        self,
        worker_id: str,
        lease_seconds: int,
        job_id: str | None = None,
    ) -> ClaimedItem | None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._recover_expired(connection)
            self._reconcile_controls(connection)
            now = iso()
            row = connection.execute(
                """
                SELECT i.*, j.kind, j.execution_mode, j.options_json
                FROM job_items i
                JOIN jobs j ON j.id = i.job_id
                WHERE i.status = 'pending'
                  AND i.available_at <= ?
                  AND j.status IN ('pending', 'running')
                  AND (? IS NULL OR i.job_id = ?)
                ORDER BY j.priority DESC, j.created_at, i.seq
                LIMIT 1
                """,
                (now, job_id, job_id),
            ).fetchone()
            if not row:
                connection.commit()
                return None
            lease_expires = iso(utc_now() + timedelta(seconds=lease_seconds))
            lease_token = uuid.uuid4().hex
            cursor = connection.execute(
                """
                UPDATE job_items
                SET status = 'running', attempts = attempts + 1, lease_owner = ?,
                    lease_token = ?, lease_expires_at = ?,
                    started_at = COALESCE(started_at, ?)
                WHERE id = ? AND status = 'pending'
                """,
                (worker_id, lease_token, lease_expires, now, row["id"]),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                return None
            connection.execute(
                """
                UPDATE jobs SET status = 'running', started_at = COALESCE(started_at, ?),
                    updated_at = ? WHERE id = ? AND status = 'pending'
                """,
                (now, now, row["job_id"]),
            )
            self._event(
                connection,
                row["job_id"],
                "item.claimed",
                {"worker_id": worker_id, "attempt": int(row["attempts"]) + 1},
                row["id"],
            )
            connection.commit()
            return ClaimedItem(
                id=row["id"],
                job_id=row["job_id"],
                seq=int(row["seq"]),
                kind=row["kind"],
                execution_mode=ExecutionMode(row["execution_mode"]),
                input=json.loads(row["input_json"]),
                options=json.loads(row["options_json"]),
                attempts=int(row["attempts"]) + 1,
                max_attempts=int(row["max_attempts"]),
                lease_owner=worker_id,
                lease_token=lease_token,
            )

    def heartbeat(self, item: ClaimedItem, lease_seconds: int) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE job_items SET lease_expires_at = ?
                WHERE id = ? AND status = 'running' AND lease_owner = ?
                    AND lease_token = ?
                """,
                (
                    iso(utc_now() + timedelta(seconds=lease_seconds)),
                    item.id,
                    item.lease_owner,
                    item.lease_token,
                ),
            )
        return cursor.rowcount == 1

    def _verify_owner(
        self, connection: sqlite3.Connection, item: ClaimedItem
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM job_items WHERE id = ?", (item.id,)
        ).fetchone()
        if not row:
            raise NotFoundError(f"item not found: {item.id}")
        if (
            row["status"] != "running"
            or row["lease_owner"] != item.lease_owner
            or row["lease_token"] != item.lease_token
        ):
            raise ConflictError(
                "stale worker result rejected because the item lease changed"
            )
        return row

    def complete_item(self, item: ClaimedItem, result: CrawlResult) -> None:
        now = iso()
        result_id = f"result_{uuid.uuid4().hex}"
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._verify_owner(connection, item)
            connection.execute(
                """
                INSERT INTO results(
                    id, job_id, item_id, input_key, schema_version,
                    data_json, evidence_json, collected_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(item_id) DO NOTHING
                """,
                (
                    result_id,
                    item.job_id,
                    item.id,
                    row["input_key"],
                    result.schema_version,
                    json.dumps(result.data, ensure_ascii=False, sort_keys=True),
                    json.dumps(result.evidence, ensure_ascii=False, sort_keys=True),
                    now,
                ),
            )
            stored_result = connection.execute(
                "SELECT id FROM results WHERE item_id = ?", (item.id,)
            ).fetchone()["id"]
            result_sinks = sorted(
                {
                    value
                    for value in item.options.get("result_sinks", [])
                    if isinstance(value, str) and value != "sqlite"
                }
            )
            for sink_name in result_sinks:
                connection.execute(
                    """
                    INSERT INTO result_outbox(
                        id, result_id, job_id, item_id, sink_name, status,
                        max_attempts, available_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)
                    ON CONFLICT(result_id, sink_name) DO NOTHING
                    """,
                    (
                        f"delivery_{uuid.uuid4().hex}",
                        stored_result,
                        item.job_id,
                        item.id,
                        sink_name,
                        self.delivery_max_attempts,
                        now,
                        now,
                        now,
                    ),
                )
            for position, followup in enumerate(result.followup_jobs):
                self._create_followup_job(
                    connection,
                    parent=item,
                    followup=followup,
                    position=position,
                )
            connection.execute(
                """
                UPDATE job_items SET status = 'succeeded', result_id = ?, finished_at = ?,
                    lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL,
                    last_error_code = NULL, last_error = NULL
                WHERE id = ?
                """,
                (stored_result, now, item.id),
            )
            self._event(
                connection,
                item.job_id,
                "item.succeeded",
                {
                    "schema_version": result.schema_version,
                    "result_sinks": ["sqlite", *result_sinks],
                    "queued_deliveries": len(result_sinks),
                },
                item.id,
            )
            self._refresh_job(connection, item.job_id)
            self._reconcile_controls(connection)
            connection.commit()

    def _create_followup_job(
        self,
        connection: sqlite3.Connection,
        *,
        parent: ClaimedItem,
        followup: FollowupJob,
        position: int,
    ) -> str:
        if not followup.inputs:
            raise ValueError("a follow-up job must contain at least one input")
        deduplicated = list(
            {value.input_key: value for value in followup.inputs}.values()
        )
        idempotency_key = f"followup:{parent.id}:{position}:{followup.kind}"
        existing = connection.execute(
            "SELECT id FROM jobs WHERE idempotency_key = ?", (idempotency_key,)
        ).fetchone()
        if existing:
            return str(existing["id"])
        now = iso()
        job_id = f"job_{uuid.uuid4().hex}"
        parent_job = connection.execute(
            "SELECT tenant_id, created_by FROM jobs WHERE id = ?",
            (parent.job_id,),
        ).fetchone()
        if parent_job is None:
            raise ValueError("parent job is unavailable")
        options = {
            "parent_job_id": parent.job_id,
            "parent_item_id": parent.id,
            "root_job_id": parent.options.get("root_job_id") or parent.job_id,
            "followup_reason": followup.reason,
        }
        if parent.options.get("result_sinks"):
            options["result_sinks"] = list(parent.options["result_sinks"])
        connection.execute(
            """
            INSERT INTO jobs(
                id, idempotency_key, tenant_id, created_by,
                kind, execution_mode, status, priority,
                options_json, total_items, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                idempotency_key,
                parent_job["tenant_id"],
                parent_job["created_by"],
                followup.kind,
                followup.execution_mode,
                followup.priority,
                json.dumps(options, ensure_ascii=False, sort_keys=True),
                len(deduplicated),
                now,
                now,
            ),
        )
        for seq, normalized in enumerate(deduplicated, start=1):
            connection.execute(
                """
                INSERT INTO job_items(
                    id, job_id, seq, input_key, input_json, status,
                    max_attempts, available_at
                ) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    f"item_{uuid.uuid4().hex}",
                    job_id,
                    seq,
                    normalized.input_key,
                    json.dumps(
                        normalized.as_dict(), ensure_ascii=False, sort_keys=True
                    ),
                    followup.max_attempts,
                    now,
                ),
            )
        self._event(
            connection,
            job_id,
            "job.created",
            {
                "items": len(deduplicated),
                "parent_job_id": parent.job_id,
                "parent_item_id": parent.id,
                "reason": followup.reason,
            },
        )
        self._event(
            connection,
            parent.job_id,
            "job.followup_created",
            {
                "child_job_id": job_id,
                "kind": followup.kind,
                "items": len(deduplicated),
                "reason": followup.reason,
            },
            parent.id,
        )
        return job_id

    def fail_item(
        self,
        item: ClaimedItem,
        *,
        code: str,
        message: str,
        retryable: bool,
        details: dict[str, Any] | None = None,
    ) -> None:
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._verify_owner(connection, item)
            should_retry = retryable and int(row["attempts"]) < int(row["max_attempts"])
            if should_retry:
                backoff_seconds = min(300, 2 ** max(0, int(row["attempts"]) - 1))
                connection.execute(
                    """
                    UPDATE job_items SET status = 'pending', available_at = ?,
                        lease_owner = NULL, lease_token = NULL,
                        lease_expires_at = NULL,
                        last_error_code = ?, last_error = ? WHERE id = ?
                    """,
                    (
                        iso(now + timedelta(seconds=backoff_seconds)),
                        code,
                        message[:2000],
                        item.id,
                    ),
                )
                event_type = "item.retry_scheduled"
                payload = {
                    "code": code,
                    "backoff_seconds": backoff_seconds,
                    "details": details or {},
                }
            else:
                connection.execute(
                    """
                    UPDATE job_items SET status = 'failed', finished_at = ?,
                        lease_owner = NULL, lease_token = NULL,
                        lease_expires_at = NULL,
                        last_error_code = ?, last_error = ? WHERE id = ?
                    """,
                    (iso(now), code, message[:2000], item.id),
                )
                event_type = "item.failed"
                payload = {
                    "code": code,
                    "retryable": retryable,
                    "details": details or {},
                }
            self._event(connection, item.job_id, event_type, payload, item.id)
            self._refresh_job(
                connection, item.job_id, last_error=(code, message[:2000])
            )
            self._reconcile_controls(connection)
            connection.commit()

    def _refresh_job(
        self,
        connection: sqlite3.Connection,
        job_id: str,
        *,
        force_status: str | None = None,
        last_error: tuple[str, str] | None = None,
    ) -> None:
        counts = {
            row["status"]: int(row["count"])
            for row in connection.execute(
                "SELECT status, COUNT(*) AS count FROM job_items WHERE job_id = ? GROUP BY status",
                (job_id,),
            ).fetchall()
        }
        total = sum(counts.values())
        open_row = connection.execute(
            """
            SELECT MIN(seq) AS first_open FROM job_items
            WHERE job_id = ? AND status IN ('pending', 'running')
            """,
            (job_id,),
        ).fetchone()
        checkpoint_seq = (
            (int(open_row["first_open"]) - 1) if open_row["first_open"] else total
        )
        status = force_status
        if not status and counts.get("pending", 0) + counts.get("running", 0) == 0:
            current_status = connection.execute(
                "SELECT status FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()["status"]
            if current_status == "cancel_requested":
                status = "cancelled"
            elif counts.get("failed", 0) == 0 and counts.get("cancelled", 0) == 0:
                status = "succeeded"
            elif counts.get("succeeded", 0) > 0:
                status = "partial"
            elif counts.get("cancelled", 0) == total:
                status = "cancelled"
            else:
                status = "failed"

        assignments = [
            "succeeded_items = ?",
            "failed_items = ?",
            "cancelled_items = ?",
            "checkpoint_seq = ?",
            "checkpoint_json = ?",
            "updated_at = ?",
        ]
        values: list[Any] = [
            counts.get("succeeded", 0),
            counts.get("failed", 0),
            counts.get("cancelled", 0),
            checkpoint_seq,
            json.dumps(
                {
                    "version": 1,
                    "contiguous_terminal_seq": checkpoint_seq,
                    "counts": counts,
                },
                sort_keys=True,
            ),
            iso(),
        ]
        if status:
            assignments.extend(["status = ?", "finished_at = ?"])
            values.extend([status, iso()])
        if status == "succeeded":
            assignments.extend(["last_error_code = NULL", "last_error = NULL"])
        if last_error:
            assignments.extend(["last_error_code = ?", "last_error = ?"])
            values.extend(last_error)
        values.append(job_id)
        connection.execute(
            f"UPDATE jobs SET {', '.join(assignments)} WHERE id = ?", values
        )
        if status in {"succeeded", "partial", "failed"}:
            self._event(connection, job_id, f"job.{status}", {"counts": counts})

    def request_pause(
        self,
        job_id: str,
        *,
        tenant_id: str | None = None,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            query = "SELECT status FROM jobs WHERE id = ?"
            params: list[Any] = [job_id]
            if tenant_id is not None:
                query += " AND tenant_id = ?"
                params.append(tenant_id)
            row = connection.execute(query, params).fetchone()
            if not row:
                raise NotFoundError(f"job not found: {job_id}")
            if row["status"] in {"pending", "running"}:
                connection.execute(
                    "UPDATE jobs SET status = 'pause_requested', updated_at = ? WHERE id = ?",
                    (iso(), job_id),
                )
                self._event(connection, job_id, "job.pause_requested")
                self._reconcile_controls(connection)
            connection.commit()
        return self.get_job(job_id, tenant_id=tenant_id)

    def resume(
        self,
        job_id: str,
        *,
        tenant_id: str | None = None,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            query = "SELECT status FROM jobs WHERE id = ?"
            params: list[Any] = [job_id]
            if tenant_id is not None:
                query += " AND tenant_id = ?"
                params.append(tenant_id)
            row = connection.execute(query, params).fetchone()
            if not row:
                raise NotFoundError(f"job not found: {job_id}")
            if row["status"] in {"paused", "pause_requested"}:
                new_status = (
                    "running"
                    if connection.execute(
                        "SELECT 1 FROM job_items WHERE job_id = ? AND status = 'running'",
                        (job_id,),
                    ).fetchone()
                    else "pending"
                )
                connection.execute(
                    "UPDATE jobs SET status = ?, updated_at = ?, finished_at = NULL WHERE id = ?",
                    (new_status, iso(), job_id),
                )
                self._event(connection, job_id, "job.resumed")
            elif row["status"] not in {"pending", "running"}:
                raise ConflictError(f"cannot resume a {row['status']} job")
            connection.commit()
        return self.get_job(job_id, tenant_id=tenant_id)

    def request_cancel(
        self,
        job_id: str,
        *,
        tenant_id: str | None = None,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            query = "SELECT status FROM jobs WHERE id = ?"
            params: list[Any] = [job_id]
            if tenant_id is not None:
                query += " AND tenant_id = ?"
                params.append(tenant_id)
            row = connection.execute(query, params).fetchone()
            if not row:
                raise NotFoundError(f"job not found: {job_id}")
            if row["status"] not in {"cancelled", "succeeded", "partial", "failed"}:
                connection.execute(
                    "UPDATE jobs SET status = 'cancel_requested', updated_at = ? WHERE id = ?",
                    (iso(), job_id),
                )
                self._event(connection, job_id, "job.cancel_requested")
                self._reconcile_controls(connection)
            connection.commit()
        return self.get_job(job_id, tenant_id=tenant_id)

    def _recover_expired_deliveries(self, connection: sqlite3.Connection) -> int:
        now = iso()
        rows = connection.execute(
            """
            SELECT id, job_id, item_id, attempts, max_attempts, lease_owner
            FROM result_outbox
            WHERE status = 'running' AND lease_expires_at <= ?
            """,
            (now,),
        ).fetchall()
        for row in rows:
            next_status = (
                "pending"
                if int(row["attempts"]) < int(row["max_attempts"])
                else "dead_letter"
            )
            connection.execute(
                """
                UPDATE result_outbox
                SET status = ?, available_at = ?, lease_owner = NULL,
                    lease_token = NULL,
                    lease_expires_at = NULL,
                    last_error = 'delivery worker lease expired', updated_at = ?
                WHERE id = ? AND status = 'running'
                """,
                (next_status, now, now, row["id"]),
            )
            self._event(
                connection,
                row["job_id"],
                "result.delivery_lease_expired",
                {
                    "delivery_id": row["id"],
                    "previous_worker": row["lease_owner"],
                    "next_status": next_status,
                },
                row["item_id"],
            )
        return len(rows)

    def recover_expired_delivery_leases(self) -> int:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            recovered = self._recover_expired_deliveries(connection)
            connection.commit()
        return recovered

    def claim_delivery(
        self,
        worker_id: str,
        lease_seconds: int,
        job_id: str | None = None,
    ) -> ClaimedDelivery | None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._recover_expired_deliveries(connection)
            now = iso()
            row = connection.execute(
                """
                SELECT o.*, r.schema_version, r.data_json, r.evidence_json,
                       r.collected_at, j.kind
                FROM result_outbox o
                JOIN results r ON r.id = o.result_id
                JOIN jobs j ON j.id = o.job_id
                WHERE o.status = 'pending'
                  AND o.available_at <= ?
                  AND o.attempts < o.max_attempts
                  AND (? IS NULL OR o.job_id = ?)
                ORDER BY o.created_at, o.id
                LIMIT 1
                """,
                (now, job_id, job_id),
            ).fetchone()
            if not row:
                connection.commit()
                return None
            lease_expires = iso(utc_now() + timedelta(seconds=lease_seconds))
            lease_token = uuid.uuid4().hex
            cursor = connection.execute(
                """
                UPDATE result_outbox
                SET status = 'running', attempts = attempts + 1,
                    lease_owner = ?, lease_token = ?, lease_expires_at = ?, updated_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (worker_id, lease_token, lease_expires, now, row["id"]),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                return None
            attempt = int(row["attempts"]) + 1
            self._event(
                connection,
                row["job_id"],
                "result.delivery_claimed",
                {
                    "delivery_id": row["id"],
                    "sink": row["sink_name"],
                    "attempt": attempt,
                    "worker_id": worker_id,
                },
                row["item_id"],
            )
            connection.commit()
            return ClaimedDelivery(
                id=str(row["id"]),
                result_id=str(row["result_id"]),
                job_id=str(row["job_id"]),
                item_id=str(row["item_id"]),
                sink_name=str(row["sink_name"]),
                kind=str(row["kind"]),
                schema_version=str(row["schema_version"]),
                data=json.loads(row["data_json"]),
                evidence=json.loads(row["evidence_json"]),
                collected_at=str(row["collected_at"]),
                attempts=attempt,
                max_attempts=int(row["max_attempts"]),
                lease_owner=worker_id,
                lease_token=lease_token,
            )

    def _verify_delivery_owner(
        self, connection: sqlite3.Connection, delivery: ClaimedDelivery
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM result_outbox WHERE id = ?", (delivery.id,)
        ).fetchone()
        if not row:
            raise NotFoundError(f"delivery not found: {delivery.id}")
        if (
            row["status"] != "running"
            or row["lease_owner"] != delivery.lease_owner
            or row["lease_token"] != delivery.lease_token
        ):
            raise ConflictError(
                "stale delivery result rejected because the delivery lease changed"
            )
        return row

    def heartbeat_delivery(self, delivery: ClaimedDelivery, lease_seconds: int) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE result_outbox SET lease_expires_at = ?, updated_at = ?
                WHERE id = ? AND status = 'running' AND lease_owner = ?
                    AND lease_token = ?
                """,
                (
                    iso(utc_now() + timedelta(seconds=lease_seconds)),
                    iso(),
                    delivery.id,
                    delivery.lease_owner,
                    delivery.lease_token,
                ),
            )
        return cursor.rowcount == 1

    def complete_delivery(
        self, delivery: ClaimedDelivery, receipt: dict[str, Any]
    ) -> None:
        now = iso()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._verify_delivery_owner(connection, delivery)
            connection.execute(
                """
                UPDATE result_outbox
                SET status = 'delivered', delivered_at = ?, lease_owner = NULL,
                    lease_token = NULL,
                    lease_expires_at = NULL, last_error = NULL,
                    receipt_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    now,
                    json.dumps(receipt, ensure_ascii=False, sort_keys=True),
                    now,
                    delivery.id,
                ),
            )
            self._event(
                connection,
                delivery.job_id,
                "result.delivered",
                {
                    "delivery_id": delivery.id,
                    "sink": delivery.sink_name,
                    "attempt": delivery.attempts,
                },
                delivery.item_id,
            )
            connection.commit()

    def fail_delivery(self, delivery: ClaimedDelivery, *, message: str) -> None:
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._verify_delivery_owner(connection, delivery)
            retry = int(row["attempts"]) < int(row["max_attempts"])
            next_status = "pending" if retry else "dead_letter"
            backoff_seconds = min(900, 2 ** max(0, int(row["attempts"]) - 1))
            available_at = (
                iso(now + timedelta(seconds=backoff_seconds)) if retry else iso(now)
            )
            connection.execute(
                """
                UPDATE result_outbox
                SET status = ?, available_at = ?, lease_owner = NULL,
                    lease_token = NULL,
                    lease_expires_at = NULL, last_error = ?, updated_at = ?
                WHERE id = ?
                """,
                (next_status, available_at, message[:1000], iso(now), delivery.id),
            )
            self._event(
                connection,
                delivery.job_id,
                "result.delivery_retry_scheduled"
                if retry
                else "result.delivery_dead_letter",
                {
                    "delivery_id": delivery.id,
                    "sink": delivery.sink_name,
                    "attempt": int(row["attempts"]),
                    "backoff_seconds": backoff_seconds if retry else None,
                },
                delivery.item_id,
            )
            connection.commit()

    def list_deliveries(
        self,
        job_id: str | None = None,
        *,
        limit: int = 100,
        tenant_id: str | None = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        query = """
            SELECT result_outbox.id, result_outbox.result_id, result_outbox.job_id,
                   result_outbox.item_id, result_outbox.sink_name,
                   result_outbox.status, result_outbox.attempts,
                   result_outbox.max_attempts, result_outbox.available_at,
                   result_outbox.delivered_at, result_outbox.last_error,
                   receipt_json, result_outbox.created_at, result_outbox.updated_at
            FROM result_outbox JOIN jobs ON jobs.id = result_outbox.job_id
        """
        params: list[Any] = []
        conditions: list[str] = []
        if job_id is not None:
            conditions.append("result_outbox.job_id = ?")
            params.append(job_id)
        if tenant_id is not None:
            conditions.append("jobs.tenant_id = ?")
            params.append(tenant_id)
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += (
            " ORDER BY result_outbox.created_at DESC, result_outbox.id DESC "
            "LIMIT ? OFFSET ?"
        )
        params.append(max(1, min(int(limit), 1000)))
        params.append(max(0, int(offset)))
        with self._connect() as connection:
            job_query = "SELECT 1 FROM jobs WHERE id = ?"
            job_params: list[Any] = [job_id]
            if tenant_id is not None:
                job_query += " AND tenant_id = ?"
                job_params.append(tenant_id)
            if (
                job_id is not None
                and not connection.execute(
                    job_query,
                    job_params,
                ).fetchone()
            ):
                raise NotFoundError(f"job not found: {job_id}")
            rows = connection.execute(query, params).fetchall()
        values: list[dict[str, Any]] = []
        for row in rows:
            value = dict(row)
            value["receipt"] = json.loads(value.pop("receipt_json"))
            values.append(value)
        return values

    def metrics(self, *, tenant_id: str | None = None) -> dict[str, Any]:
        with self._connect() as connection:
            where = " WHERE tenant_id = ?" if tenant_id is not None else ""
            params: tuple[Any, ...] = (tenant_id,) if tenant_id is not None else ()
            jobs = {
                row["status"]: int(row["count"])
                for row in connection.execute(
                    f"SELECT status, COUNT(*) AS count FROM jobs{where} GROUP BY status",
                    params,
                ).fetchall()
            }
            item_statuses = {
                row["status"]: int(row["count"])
                for row in connection.execute(
                    f"""
                    SELECT job_items.status, COUNT(*) AS count
                    FROM job_items JOIN jobs ON jobs.id = job_items.job_id
                    {"WHERE jobs.tenant_id = ?" if tenant_id is not None else ""}
                    GROUP BY job_items.status
                    """,
                    params,
                ).fetchall()
            }
            attempt_row = connection.execute(
                f"""
                SELECT COALESCE(SUM(job_items.attempts), 0) AS attempts_total,
                       COALESCE(SUM(CASE WHEN job_items.attempts > 1 THEN 1 ELSE 0 END), 0)
                           AS retried_items
                FROM job_items JOIN jobs ON jobs.id = job_items.job_id
                {"WHERE jobs.tenant_id = ?" if tenant_id is not None else ""}
                """,
                params,
            ).fetchone()
            failures = [
                {"code": row["last_error_code"], "count": int(row["count"])}
                for row in connection.execute(
                    f"""
                    SELECT job_items.last_error_code, COUNT(*) AS count
                    FROM job_items JOIN jobs ON jobs.id = job_items.job_id
                    WHERE job_items.last_error_code IS NOT NULL
                    {"AND jobs.tenant_id = ?" if tenant_id is not None else ""}
                    GROUP BY job_items.last_error_code
                    ORDER BY count DESC
                    """,
                    params,
                ).fetchall()
            ]
            result_count_row = connection.execute(
                f"""
                SELECT COUNT(*) AS count FROM results
                JOIN jobs ON jobs.id = results.job_id
                {"WHERE jobs.tenant_id = ?" if tenant_id is not None else ""}
                """,
                params,
            ).fetchone()
            result_rows = connection.execute(
                f"""
                SELECT results.data_json FROM results
                JOIN jobs ON jobs.id = results.job_id
                {"WHERE jobs.tenant_id = ?" if tenant_id is not None else ""}
                ORDER BY results.collected_at DESC
                LIMIT 10000
                """,
                params,
            ).fetchall()
            deliveries = {
                row["status"]: int(row["count"])
                for row in connection.execute(
                    f"""
                    SELECT result_outbox.status, COUNT(*) AS count
                    FROM result_outbox JOIN jobs ON jobs.id = result_outbox.job_id
                    {"WHERE jobs.tenant_id = ?" if tenant_id is not None else ""}
                    GROUP BY result_outbox.status
                    """,
                    params,
                ).fetchall()
            }
        coverages: list[float] = []
        parser_versions: dict[str, int] = {}
        for row in result_rows:
            data = json.loads(row["data_json"])
            quality = data.get("quality") or {}
            if isinstance(quality.get("core_field_coverage"), (int, float)):
                coverages.append(float(quality["core_field_coverage"]))
            version = str(data.get("parser_version") or "unknown")
            parser_versions[version] = parser_versions.get(version, 0) + 1
        return {
            "jobs_by_status": jobs,
            "job_items_by_status": item_statuses,
            "crawl_attempts_total": int(attempt_row["attempts_total"]),
            "retried_items": int(attempt_row["retried_items"]),
            "failure_reasons": failures,
            "results": int(result_count_row["count"]),
            "quality_sample_size": len(result_rows),
            "quality_sample_limit": 10_000,
            "average_core_field_coverage": round(sum(coverages) / len(coverages), 4)
            if coverages
            else None,
            "parser_versions": parser_versions,
            "deliveries_by_status": deliveries,
        }

    def operational_counts(self, *, tenant_id: str | None = None) -> dict[str, Any]:
        """Return lightweight queue counts suitable for frequent health probes."""
        params: tuple[Any, ...] = (tenant_id,) if tenant_id is not None else ()
        with self._connect() as connection:
            jobs = {
                row["status"]: int(row["count"])
                for row in connection.execute(
                    f"""
                    SELECT status, COUNT(*) AS count FROM jobs
                    {"WHERE tenant_id = ?" if tenant_id is not None else ""}
                    GROUP BY status
                    """,
                    params,
                ).fetchall()
            }
            deliveries = {
                row["status"]: int(row["count"])
                for row in connection.execute(
                    f"""
                    SELECT result_outbox.status, COUNT(*) AS count
                    FROM result_outbox JOIN jobs ON jobs.id = result_outbox.job_id
                    {"WHERE jobs.tenant_id = ?" if tenant_id is not None else ""}
                    GROUP BY result_outbox.status
                    """,
                    params,
                ).fetchall()
            }
        return {
            "jobs_by_status": jobs,
            "deliveries_by_status": deliveries,
        }

    def healthcheck(self) -> bool:
        with self._connect() as connection:
            row = connection.execute("SELECT 1 AS ok").fetchone()
        return bool(row and row["ok"] == 1)

    def start_mcp_audit(
        self,
        *,
        audit_id: str,
        tenant_id: str,
        actor_id: str,
        client_id: str,
        tool_name: str,
        arguments_sha256: str,
        request_id: str | None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO mcp_audit_events(
                    id, tenant_id, actor_id, client_id, tool_name,
                    arguments_sha256, request_id, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'started', ?)
                """,
                (
                    audit_id,
                    tenant_id,
                    actor_id,
                    client_id,
                    tool_name,
                    arguments_sha256,
                    request_id,
                    iso(),
                ),
            )

    def finish_mcp_audit(
        self,
        audit_id: str,
        *,
        status: str,
        latency_ms: int,
        error_code: str | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE mcp_audit_events
                SET status = ?, error_code = ?, latency_ms = ?, finished_at = ?
                WHERE id = ?
                """,
                (status, error_code, max(0, latency_ms), iso(), audit_id),
            )

    def list_mcp_audit_events(
        self,
        *,
        tenant_id: str | None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        where = "WHERE tenant_id = ?" if tenant_id is not None else ""
        params: list[Any] = [tenant_id] if tenant_id is not None else []
        params.extend(
            [
                max(1, min(int(limit), 500)),
                max(0, int(offset)),
            ]
        )
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT id, tenant_id, actor_id, client_id, tool_name,
                       arguments_sha256, request_id, status, error_code,
                       latency_ms, created_at, finished_at
                FROM mcp_audit_events
                {where}
                ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def consume_mcp_approval(
        self,
        *,
        nonce: str,
        tenant_id: str,
        actor_id: str,
        action: str,
        parameters_sha256: str,
        expires_at: str,
    ) -> None:
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO mcp_approval_consumptions(
                        nonce, tenant_id, actor_id, action,
                        parameters_sha256, expires_at, consumed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        nonce,
                        tenant_id,
                        actor_id,
                        action,
                        parameters_sha256,
                        expires_at,
                        iso(),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise ConflictError("approval receipt has already been consumed") from exc

    def prune_mcp_security_state(self, *, audit_retention_days: int) -> dict[str, int]:
        audit_cutoff = iso(utc_now() - timedelta(days=max(1, audit_retention_days)))
        now = iso()
        with self._connect() as connection:
            audit = connection.execute(
                "DELETE FROM mcp_audit_events WHERE created_at < ?",
                (audit_cutoff,),
            ).rowcount
            approvals = connection.execute(
                "DELETE FROM mcp_approval_consumptions WHERE expires_at < ?",
                (now,),
            ).rowcount
        return {
            "audit_events_deleted": max(0, int(audit)),
            "approval_receipts_deleted": max(0, int(approvals)),
        }


def stable_idempotency_key(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "auto_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()
