#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import secrets
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from amazon_crawler.infra.legacy_compat import ALLOWED_TABLES, LEGACY_TASKS
from amazon_crawler.infra.legacy_runtime_config import load_legacy_runtime_config


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


def _mysql_probe(url: str) -> dict[str, object]:
    import sqlalchemy

    engine = sqlalchemy.create_engine(
        url,
        hide_parameters=True,
        pool_pre_ping=True,
        connect_args={"connect_timeout": 3},
    )
    token = secrets.token_hex(16)
    temporary_table = "crawler_v2_loopback_probe"
    try:
        with engine.connect() as connection:
            select_ok = connection.exec_driver_sql("SELECT 1").scalar() == 1
            connection.exec_driver_sql(
                f"CREATE TEMPORARY TABLE `{temporary_table}` ("
                "probe_id VARCHAR(64) PRIMARY KEY, created_at VARCHAR(64) NOT NULL)"
            )
            connection.exec_driver_sql(
                f"INSERT INTO `{temporary_table}` (probe_id, created_at) VALUES (%s, %s)",
                (token, _iso_now()),
            )
            roundtrip_ok = (
                connection.exec_driver_sql(
                    f"SELECT probe_id FROM `{temporary_table}` WHERE probe_id = %s",
                    (token,),
                ).scalar()
                == token
            )
            inspector = sqlalchemy.inspect(connection)
            existing = sorted(
                table for table in ALLOWED_TABLES if inspector.has_table(table)
            )
            missing = sorted(set(ALLOWED_TABLES) - set(existing))
            missing_task_state_columns: list[str] = []
            for task_table, _, _ in LEGACY_TASKS.values():
                if task_table not in existing:
                    continue
                columns = {
                    value["name"] for value in inspector.get_columns(task_table)
                }
                if not {"id", "state"}.issubset(columns):
                    missing_task_state_columns.append(task_table)
        return {
            "select_1": bool(select_ok),
            "temporary_write_roundtrip": bool(roundtrip_ok),
            "temporary_table_persisted": False,
            "allowlisted_tables_expected": len(ALLOWED_TABLES),
            "allowlisted_tables_found": len(existing),
            "missing_tables": missing,
            "task_tables_missing_id_or_state": sorted(missing_task_state_columns),
        }
    finally:
        engine.dispose()


def _redis_probe(url: str) -> dict[str, object]:
    import redis

    client = redis.Redis.from_url(
        url,
        decode_responses=False,
        socket_connect_timeout=3,
        socket_timeout=3,
    )
    probe = secrets.token_hex(16)
    key_hash = hashlib.sha256(probe.encode("utf-8")).hexdigest()
    marker = f"amazon_crawler_v2:probe:{key_hash}:marker"
    values = f"amazon_crawler_v2:probe:{key_hash}:values"
    script = """
        redis.call('SET', KEYS[1], ARGV[1], 'EX', 60)
        redis.call('RPUSH', KEYS[2], ARGV[1])
        redis.call('EXPIRE', KEYS[2], 60)
        return {redis.call('GET', KEYS[1]), redis.call('LINDEX', KEYS[2], 0)}
    """
    reply = None
    cleanup_count = 0
    try:
        ping = bool(client.ping())
        reply = client.eval(script, 2, marker, values, probe)
        roundtrip_ok = (
            isinstance(reply, (list, tuple))
            and len(reply) == 2
            and all(
                (value.decode("utf-8") if isinstance(value, bytes) else value) == probe
                for value in reply
            )
        )
    finally:
        cleanup_count = int(client.delete(marker, values))
        client.close()
    return {
        "ping": ping,
        "lua_write_roundtrip": bool(roundtrip_ok),
        "probe_keys_removed": cleanup_count == 2,
        "persistent_probe_keys": 0 if cleanup_count == 2 else "unknown",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--legacy-config", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--confirm-loopback-write", action="store_true")
    args = parser.parse_args()
    if not args.confirm_loopback_write:
        raise SystemExit("--confirm-loopback-write is required")

    config_path = args.legacy_config.resolve()
    runtime = load_legacy_runtime_config(config_path)
    if not runtime.mysql_url or not runtime.result_redis_url:
        raise SystemExit("legacy config does not provide both loopback write targets")
    if runtime.target_scopes.get("mysql") != "loopback" or runtime.target_scopes.get(
        "result_redis"
    ) != "loopback":
        raise SystemExit("bridge roundtrip accepts loopback write targets only")

    report = {
        "schema_version": "amazon-crawler.legacy-bridge-roundtrip.v1",
        "generated_at": _iso_now(),
        "authorization": "explicit_confirm_loopback_write",
        "legacy_config": {
            "sha256": hashlib.sha256(config_path.read_bytes()).hexdigest(),
            "imports_executed": False,
            "values_disclosed": False,
            "target_scopes": runtime.target_scopes,
        },
        "mysql": _mysql_probe(runtime.mysql_url),
        "redis": _redis_probe(runtime.result_redis_url),
    }
    report["ok"] = bool(
        report["mysql"]["select_1"]
        and report["mysql"]["temporary_write_roundtrip"]
        and report["redis"]["ping"]
        and report["redis"]["lua_write_roundtrip"]
        and report["redis"]["probe_keys_removed"]
    )

    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    secret_candidates = {
        value
        for url in (runtime.mysql_url, runtime.result_redis_url)
        for value in (urlsplit(url).username, urlsplit(url).password)
        if value
    }
    if any(secret in rendered for secret in secret_candidates):
        raise RuntimeError("secret boundary check rejected the bridge report")
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rendered + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "ok": report["ok"],
                "receipt": str(output),
                "mysql_temporary_write": report["mysql"]["temporary_write_roundtrip"],
                "redis_lua_roundtrip": report["redis"]["lua_write_roundtrip"],
                "missing_legacy_tables": len(report["mysql"]["missing_tables"]),
                "values_disclosed": False,
            },
            sort_keys=True,
        )
    )
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
