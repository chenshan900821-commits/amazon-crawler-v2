#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from amazon_crawler.infra.legacy_compat import ALLOWED_TABLES
from amazon_crawler.infra.legacy_runtime_config import load_legacy_runtime_config


def _public_column(column: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": str(column["name"]),
        "type": str(column["type"]),
        "nullable": bool(column.get("nullable", True)),
        "default_present": column.get("default") is not None,
        "autoincrement": bool(column.get("autoincrement", False)),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Inspect allowlisted legacy table schemas without reading row values."
    )
    parser.add_argument("--legacy-config", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    runtime = load_legacy_runtime_config(args.legacy_config.resolve())
    if not runtime.mysql_url:
        raise SystemExit("legacy MySQL is not configured")

    import sqlalchemy

    engine = sqlalchemy.create_engine(
        runtime.mysql_url,
        hide_parameters=True,
        pool_pre_ping=True,
        connect_args={"connect_timeout": 3},
    )
    tables: dict[str, Any] = {}
    try:
        inspector = sqlalchemy.inspect(engine)
        existing = set(inspector.get_table_names())
        for table_name in sorted(ALLOWED_TABLES):
            if table_name not in existing:
                tables[table_name] = {"exists": False}
                continue
            primary_key = inspector.get_pk_constraint(table_name) or {}
            unique_constraints = inspector.get_unique_constraints(table_name)
            indexes = inspector.get_indexes(table_name)
            tables[table_name] = {
                "exists": True,
                "columns": [
                    _public_column(column)
                    for column in inspector.get_columns(table_name)
                ],
                "primary_key": list(primary_key.get("constrained_columns") or []),
                "unique_constraints": sorted(
                    [
                        list(value.get("column_names") or [])
                        for value in unique_constraints
                        if value.get("column_names")
                    ]
                ),
                "unique_indexes": sorted(
                    [
                        list(value.get("column_names") or [])
                        for value in indexes
                        if value.get("unique") and value.get("column_names")
                    ]
                ),
            }
    finally:
        engine.dispose()

    report = {
        "schema_version": "amazon-crawler.legacy-table-contracts.v1",
        "target_scope": runtime.target_scopes.get("mysql"),
        "values_disclosed": False,
        "tables": tables,
    }
    if args.output:
        output = args.output.resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
