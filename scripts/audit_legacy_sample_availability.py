#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from amazon_crawler.infra.legacy_compat import LEGACY_TASKS
from amazon_crawler.infra.legacy_runtime_config import load_legacy_runtime_config
from scripts.collect_controlled_v2_shadow import _atomic_write_outside_project


def main() -> int:
    parser = argparse.ArgumentParser()
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
    counts: dict[str, dict[str, int]] = {}
    marketplaces: dict[str, dict[str, int]] = {}
    try:
        metadata = sqlalchemy.MetaData()
        with engine.connect() as connection:
            for task_name, (table_name, _, _) in LEGACY_TASKS.items():
                table = sqlalchemy.Table(
                    table_name,
                    metadata,
                    autoload_with=connection,
                    extend_existing=True,
                )
                rows = connection.execute(
                    sqlalchemy.select(
                        table.c.state,
                        sqlalchemy.func.count().label("count"),
                    )
                    .group_by(table.c.state)
                    .order_by(table.c.state)
                ).all()
                counts[task_name] = {
                    str(state): int(count) for state, count in rows
                }
                if "market_id" in table.c:
                    market_rows = connection.execute(
                        sqlalchemy.select(
                            table.c.market_id,
                            sqlalchemy.func.count().label("count"),
                        ).group_by(table.c.market_id)
                    ).all()
                    marketplaces[task_name] = {
                        str(marketplace): int(count)
                        for marketplace, count in market_rows
                        if marketplace is not None
                    }
    finally:
        engine.dispose()

    availability = {
        task: {
            "success": values.get("1", 0),
            "no_result_or_business_terminal": sum(
                count
                for state, count in values.items()
                if state in {"-2", "-3", "-4", "-5"}
            ),
            # -1 proves only that the old retry budget was exhausted. The
            # task table does not retain an upstream status/body hash, so it
            # cannot prove a real throttle response by itself.
            "retry_exhausted_unclassified": values.get("-1", 0),
            "throttle_with_response_evidence": 0,
        }
        for task, values in counts.items()
    }
    report = {
        "schema_version": "amazon-crawler.legacy-sample-availability.v1",
        "target_scope": runtime.target_scopes.get("mysql"),
        "values_disclosed": False,
        "counts_by_state": counts,
        "counts_by_marketplace": marketplaces,
        "case_availability": availability,
        "throttle_requires_upstream_response_evidence": True,
        "all_cases_available": all(
            cases["success"] > 0
            and cases["no_result_or_business_terminal"] > 0
            and cases["throttle_with_response_evidence"] > 0
            for cases in availability.values()
        ),
    }
    if args.output:
        _atomic_write_outside_project(
            args.output,
            report,
            project_root=Path(__file__).resolve().parents[1],
        )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
