#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from amazon_crawler.infra.legacy_runtime_config import load_legacy_runtime_config


TASK_TABLE = "amazon_reviews_task"
RESULT_TABLE = "amazon_reviews"
TASK_COLUMNS = {
    "id",
    "market_id",
    "asin",
    "post_code",
    "add_date",
    "state",
    "update_time",
}
RESULT_COLUMNS = {
    "id",
    "market_id",
    "asin",
    "review_title",
    "review_text",
    "imgs",
    "review_stars",
    "review_date",
    "attributes",
    "video",
    "crawl_date",
    "task_date",
    "dedupe_hash",
}

CREATE_TASK_TABLE = f"""
CREATE TABLE IF NOT EXISTS `{TASK_TABLE}` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `market_id` VARCHAR(255) NOT NULL,
  `asin` VARCHAR(255) NOT NULL,
  `post_code` VARCHAR(255) NULL DEFAULT '',
  `add_date` VARCHAR(255) NULL DEFAULT '1970-01-01',
  `state` INT NULL DEFAULT 0,
  `update_time` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_amazon_reviews_task_identity`
    (`market_id`(64), `asin`(32), `post_code`(64), `add_date`(64)),
  KEY `ix_amazon_reviews_task_state_id` (`state`, `id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""

CREATE_RESULT_TABLE = f"""
CREATE TABLE IF NOT EXISTS `{RESULT_TABLE}` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `market_id` VARCHAR(255) NULL,
  `asin` VARCHAR(255) NULL,
  `review_title` TEXT NULL,
  `review_text` LONGTEXT NULL,
  `imgs` JSON NULL,
  `review_stars` VARCHAR(255) NULL,
  `review_date` VARCHAR(255) NULL,
  `attributes` TEXT NULL,
  `video` TEXT NULL,
  `crawl_date` VARCHAR(255) NULL,
  `task_date` VARCHAR(255) NULL,
  `dedupe_hash` CHAR(64) CHARACTER SET ascii
    GENERATED ALWAYS AS (
      SHA2(CONCAT_WS(CHAR(31),
        COALESCE(`market_id`, ''), COALESCE(`asin`, ''),
        COALESCE(`review_title`, ''), COALESCE(`review_text`, ''),
        COALESCE(`review_date`, ''), COALESCE(`attributes`, ''),
        COALESCE(`video`, ''), COALESCE(`task_date`, '')
      ), 256)
    ) STORED,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_amazon_reviews_dedupe_hash` (`dedupe_hash`),
  KEY `ix_amazon_reviews_asin_task_date` (`market_id`, `asin`, `task_date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""


def _columns(inspector, table: str) -> set[str]:
    return {value["name"] for value in inspector.get_columns(table)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--legacy-config", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-loopback-write", action="store_true")
    args = parser.parse_args()
    if args.apply and not args.confirm_loopback_write:
        raise SystemExit("--apply requires --confirm-loopback-write")

    config_path = args.legacy_config.resolve()
    runtime = load_legacy_runtime_config(config_path)
    if not runtime.mysql_url or runtime.target_scopes.get("mysql") != "loopback":
        raise SystemExit("review table migration accepts a loopback MySQL target only")

    import sqlalchemy

    engine = sqlalchemy.create_engine(
        runtime.mysql_url,
        hide_parameters=True,
        pool_pre_ping=True,
        connect_args={"connect_timeout": 3},
    )
    try:
        before_inspector = sqlalchemy.inspect(engine)
        existed_before = {
            TASK_TABLE: before_inspector.has_table(TASK_TABLE),
            RESULT_TABLE: before_inspector.has_table(RESULT_TABLE),
        }
        if args.apply:
            with engine.begin() as connection:
                connection.exec_driver_sql(CREATE_TASK_TABLE)
                connection.exec_driver_sql(CREATE_RESULT_TABLE)
        inspector = sqlalchemy.inspect(engine)
        tables = {
            TASK_TABLE: inspector.has_table(TASK_TABLE),
            RESULT_TABLE: inspector.has_table(RESULT_TABLE),
        }
        task_columns = _columns(inspector, TASK_TABLE) if tables[TASK_TABLE] else set()
        result_columns = _columns(inspector, RESULT_TABLE) if tables[RESULT_TABLE] else set()
    finally:
        engine.dispose()

    report = {
        "schema_version": "amazon-crawler.legacy-review-schema.v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "mode": "apply" if args.apply else "inspect",
        "authorization": "explicit_confirm_loopback_write" if args.apply else "read_only",
        "legacy_config": {
            "sha256": hashlib.sha256(config_path.read_bytes()).hexdigest(),
            "imports_executed": False,
            "values_disclosed": False,
            "mysql_scope": runtime.target_scopes.get("mysql"),
        },
        "existed_before": existed_before,
        "exists_after": tables,
        "created": {
            table: bool(args.apply and tables[table] and not existed_before[table])
            for table in (TASK_TABLE, RESULT_TABLE)
        },
        "missing_required_columns": {
            TASK_TABLE: sorted(TASK_COLUMNS - task_columns),
            RESULT_TABLE: sorted(RESULT_COLUMNS - result_columns),
        },
    }
    report["ok"] = all(tables.values()) and not any(
        report["missing_required_columns"].values()
    )
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "ok": report["ok"],
                "mode": report["mode"],
                "created": report["created"],
                "receipt": str(output),
                "values_disclosed": False,
            },
            sort_keys=True,
        )
    )
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
