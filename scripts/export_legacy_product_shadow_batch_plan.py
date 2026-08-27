#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from amazon_crawler.infra.legacy_compat import LEGACY_TASKS
from amazon_crawler.infra.legacy_runtime_config import load_legacy_runtime_config
from amazon_crawler.plugins.amazon_parser import (
    LEGACY_DIMENSION_FIELDS,
    LEGACY_PRODUCT_FIELDS,
)
from amazon_crawler.plugins.marketplaces import AMAZON_ID_TO_MARKETPLACE
from scripts.collect_controlled_v2_shadow import (
    _atomic_write_outside_project,
    validate_shadow_batch_plan,
)
from scripts.compile_controlled_evidence import ControlledEvidenceError


PRODUCT_TASKS = {
    "product_jp": "product",
    "product_hw_jp": "product_hw",
    "product_time_jp": "product_time",
}
RECEIPT_NAMES = (
    "checkpoint_recovery",
    "cookie_proxy_redaction",
    "legacy_bridge_roundtrip",
)


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _json_value(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(child) for child in value]
    return str(value)


def _filtered(row: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    return {field: _json_value(row.get(field)) for field in fields}


def _read_receipts(directory: Path) -> dict[str, dict[str, Any]]:
    receipts: dict[str, dict[str, Any]] = {}
    for name in RECEIPT_NAMES:
        path = directory / f"{name}.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ControlledEvidenceError(
                f"controlled receipt {name} could not be read"
            ) from exc
        if not isinstance(value, dict):
            raise ControlledEvidenceError(f"controlled receipt {name} is invalid")
        receipts[name] = value
    return receipts


def export_plan(
    *,
    mysql_url: str,
    task_name: str,
    run_id: str,
    batch_id: str,
    authorization_reference: str,
    check_receipts: dict[str, dict[str, Any]],
    allow_historical_fallback: bool = False,
) -> tuple[dict[str, Any], str]:
    import sqlalchemy

    if task_name not in PRODUCT_TASKS:
        raise ControlledEvidenceError("unsupported legacy product task")
    task_table_name, result_table_name, _ = LEGACY_TASKS[task_name]
    connect_args = (
        {"connect_timeout": 3}
        if sqlalchemy.engine.make_url(mysql_url).get_backend_name() in {"mysql", "mariadb"}
        else {}
    )
    engine = sqlalchemy.create_engine(
        mysql_url,
        hide_parameters=True,
        pool_pre_ping=True,
        connect_args=connect_args,
    )
    selected_task: dict[str, Any] | None = None
    result_row: dict[str, Any] | None = None
    dimension_rows: list[dict[str, Any]] = []
    join_strategy = "task_id"
    success_count = 0
    result_count = 0
    try:
        metadata = sqlalchemy.MetaData()
        with engine.connect() as connection:
            task_table = sqlalchemy.Table(
                task_table_name,
                metadata,
                autoload_with=connection,
            )
            result_table = sqlalchemy.Table(
                result_table_name,
                metadata,
                autoload_with=connection,
            )
            success_count = int(
                connection.scalar(
                    sqlalchemy.select(sqlalchemy.func.count())
                    .select_from(task_table)
                    .where(task_table.c.state == 1)
                )
                or 0
            )
            result_count = int(
                connection.scalar(
                    sqlalchemy.select(sqlalchemy.func.count()).select_from(result_table)
                )
                or 0
            )
            candidates = connection.execute(
                sqlalchemy.select(task_table)
                .where(task_table.c.state == 1)
                .order_by(task_table.c.id.desc())
                .limit(500)
            ).mappings()
            for candidate in candidates:
                task = dict(candidate)
                result = connection.execute(
                    sqlalchemy.select(result_table)
                    .where(result_table.c.task_id == str(task["id"]))
                    .limit(1)
                ).mappings().first()
                if result is not None:
                    selected_task = task
                    result_row = dict(result)
                    break
            if (
                allow_historical_fallback
                and (selected_task is None or result_row is None)
            ):
                candidates = connection.execute(
                    sqlalchemy.select(task_table)
                    .where(task_table.c.state == 1)
                    .order_by(task_table.c.id.desc())
                    .limit(500)
                ).mappings()
                for candidate in candidates:
                    task = dict(candidate)
                    conditions = [
                        result_table.c.asin == str(task.get("asin") or ""),
                        result_table.c.marketplace_id
                        == str(task.get("market_id") or ""),
                    ]
                    if "zipcode" in result_table.c and task.get("post_code"):
                        conditions.append(
                            result_table.c.zipcode == str(task["post_code"])
                        )
                    statement = sqlalchemy.select(result_table).where(*conditions)
                    if "created_at" in result_table.c:
                        statement = statement.order_by(result_table.c.created_at.desc())
                    result = connection.execute(statement.limit(1)).mappings().first()
                    if result is not None:
                        selected_task = task
                        result_row = dict(result)
                        join_strategy = "asin_market_postal_latest"
                        break
            if selected_task is None or result_row is None:
                qualification = (
                    "no exact task_id join is available; historical ASIN fallback "
                    "is disabled because it cannot prove same-response parity"
                    if not allow_historical_fallback
                    else "no joined successful legacy sample is available"
                )
                raise ControlledEvidenceError(
                    f"{qualification} for "
                    f"{task_name} (successful_tasks={success_count}, "
                    f"result_rows={result_count})"
                )
            if task_name != "product_time_jp":
                dimension_table = sqlalchemy.Table(
                    "amazon_dimensions_detail",
                    metadata,
                    autoload_with=connection,
                )
                dimension_rows = [
                    dict(row)
                    for row in connection.execute(
                        sqlalchemy.select(dimension_table)
                        .where(
                            dimension_table.c.task_id
                            == str(result_row.get("task_id") or selected_task["id"])
                        )
                        .order_by(dimension_table.c.id)
                    ).mappings()
                ]
    finally:
        engine.dispose()

    market_id = str(selected_task.get("market_id") or "")
    market = AMAZON_ID_TO_MARKETPLACE.get(market_id)
    postal_code = str(selected_task.get("post_code") or "").strip()
    asin = str(selected_task.get("asin") or "").strip().upper()
    if market is None or not postal_code or len(asin) != 10:
        raise ControlledEvidenceError(
            "legacy product sample lacks a supported marketplace, postal code, or ASIN"
        )
    input_value = {
        "id": str(selected_task["id"]),
        "market_id": market_id,
        "asin": asin,
        "post_code": postal_code,
        "add_date": _json_value(selected_task.get("add_date")),
    }
    projection = {
        "result_rows": [_filtered(result_row, LEGACY_PRODUCT_FIELDS)],
        "dimension_rows": [
            _filtered(row, LEGACY_DIMENSION_FIELDS) for row in dimension_rows
        ],
        "child_task_rows": [],
        "product_task_rows": [],
    }
    plan = {
        "schema_version": "controlled-shadow-batch-plan.v1",
        "batch_id": batch_id,
        "run_id": run_id,
        "authorization_reference": authorization_reference,
        "environment": "isolated",
        "source_evidence": {
            "source": "legacy_loopback_mysql",
            "join_strategy": join_strategy,
            "task_identity_sha256": hashlib.sha256(
                f"{task_name}:{selected_task['id']}".encode("utf-8")
            ).hexdigest(),
            "result_identity_sha256": hashlib.sha256(
                f"{result_table_name}:{result_row.get('task_id')}".encode("utf-8")
            ).hexdigest(),
        },
        "check_receipts": check_receipts,
        "scenarios": [
            {
                "kind": PRODUCT_TASKS[task_name],
                "case": "success",
                "authorized": True,
                "marketplace_id": market.id,
                "postal_code": postal_code,
                "input": input_value,
                "legacy": {"state": 1, "projection": projection},
            }
        ],
    }
    validate_shadow_batch_plan(plan)
    task_hash = plan["source_evidence"]["task_identity_sha256"]
    return plan, task_hash


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Export one joined legacy product success observation as a private "
            "incremental controlled shadow batch plan."
        )
    )
    parser.add_argument("--legacy-config", required=True, type=Path)
    parser.add_argument("--task-name", choices=sorted(PRODUCT_TASKS), default="product_jp")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--authorization-reference", required=True)
    parser.add_argument("--receipts-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--allow-historical-fallback",
        action="store_true",
        help=(
            "diagnostic only: join the latest result by ASIN/market/postal code "
            "when task_id lineage is absent; such a sample is not same-response proof"
        ),
    )
    args = parser.parse_args()
    project_root = Path(__file__).resolve().parents[1]
    try:
        runtime = load_legacy_runtime_config(args.legacy_config.resolve())
        if not runtime.mysql_url:
            raise ControlledEvidenceError("legacy MySQL is not configured")
        plan, task_hash = export_plan(
            mysql_url=runtime.mysql_url,
            task_name=args.task_name,
            run_id=args.run_id,
            batch_id=args.batch_id,
            authorization_reference=args.authorization_reference,
            check_receipts=_read_receipts(args.receipts_dir.resolve()),
            allow_historical_fallback=args.allow_historical_fallback,
        )
        _atomic_write_outside_project(
            args.output,
            plan,
            project_root=project_root,
        )
    except (OSError, ControlledEvidenceError, RuntimeError, ValueError) as exc:
        message = (
            str(exc)
            if isinstance(exc, ControlledEvidenceError)
            else "legacy product shadow plan export failed"
        )
        print(
            json.dumps(
                {"ok": False, "error": {"type": type(exc).__name__, "message": message}},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2
    print(
        json.dumps(
            {
                "ok": True,
                "output": str(args.output.resolve()),
                "task_name": args.task_name,
                "task_identity_sha256": task_hash,
                "scenario_count": 1,
                "raw_values_disclosed": False,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
