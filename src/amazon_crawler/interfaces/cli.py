from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

import uvicorn

from amazon_crawler.bootstrap import build_application
from amazon_crawler.application.preflight import configuration_report
from amazon_crawler.application.scoped_runner import run_scoped_job
from amazon_crawler.config import Settings
from amazon_crawler.domain.errors import CrawlerError, ValidationError
from amazon_crawler.infra.legacy_compat import (
    LEGACY_TASKS,
    LegacyMySQLResultWriter,
    LegacyRedisResultBuffer,
    LegacyTaskImporter,
    SQLAlchemyLegacyGateway,
    assert_legacy_job_compatibility,
    legacy_state_for,
)
from amazon_crawler.plugins.marketplaces import marketplace_default_postal_code


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _public_error(exc: Exception) -> dict[str, str]:
    return {
        "type": exc.__class__.__name__,
        "message": str(exc) if isinstance(exc, CrawlerError) else "operation failed",
    }


def _add_job_arguments(command: argparse.ArgumentParser) -> None:
    command.add_argument("inputs", nargs="*")
    command.add_argument("--kind", default="amazon.product")
    command.add_argument(
        "--input-json",
        action="append",
        default=[],
        help="repeatable JSON object input for search, category, rank, or merchant tasks",
    )
    command.add_argument("--marketplace", required=False)
    command.add_argument("--postal-code")
    command.add_argument(
        "--mode",
        choices=["standard", "overseas", "realtime"],
        default="standard",
    )
    command.add_argument("--priority", type=int, default=0)
    command.add_argument(
        "--max-attempts",
        type=int,
        help="override the legacy-compatible default (5, or 11 for search_hour)",
    )
    command.add_argument("--idempotency-key")
    command.add_argument(
        "--result-sink",
        action="append",
        choices=["sqlite", "jsonl", "legacy_redis", "legacy_mysql"],
        help="repeat to fan one result out to multiple configured destinations",
    )
    command.add_argument(
        "--confirm-external-result-write",
        action="store_true",
        help="confirm configured Redis/MySQL result delivery",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="amazon-crawler")
    parser.add_argument("--db", type=Path, help="override CRAWLER_DB_PATH")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="initialize the state database")
    serve = sub.add_parser("serve", help="run the API, UI, and optional local worker")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", default=3000, type=int)

    worker = sub.add_parser("worker", help="run a dedicated worker")
    worker.add_argument("--once", action="store_true", help="drain ready work and exit")
    worker.add_argument("--max-items", type=int)
    worker.add_argument("--max-deliveries", type=int)

    create = sub.add_parser("create", help="queue an idempotent crawl job")
    _add_job_arguments(create)
    run = sub.add_parser(
        "run",
        help="create a job, run a job-scoped local worker, and return its results",
    )
    _add_job_arguments(run)
    run.add_argument(
        "--timeout-seconds",
        type=float,
        default=600.0,
        help="soft wait limit; an in-flight request is allowed to settle",
    )

    listing = sub.add_parser("list", help="list recent jobs")
    listing.add_argument("--status")
    listing.add_argument("--limit", type=int, default=50)

    for name in (
        "show",
        "pause",
        "resume",
        "cancel",
        "results",
        "events",
        "deliveries",
    ):
        command = sub.add_parser(name)
        command.add_argument("job_id")
        if name in {"results", "events", "deliveries"}:
            command.add_argument("--limit", type=int, default=100)

    sub.add_parser(
        "doctor",
        help="check required runtime configuration without disclosing or contacting secrets",
    )
    sub.add_parser("capabilities")
    sub.add_parser("metrics")
    cookie_fill = sub.add_parser(
        "cookie-fill",
        help="explicitly create validated postal-code cookies in the configured Redis pool",
    )
    cookie_fill.add_argument("--marketplace", required=True)
    cookie_fill.add_argument(
        "--postal-code",
        help="optional operator override; defaults to the configured marketplace delivery region",
    )
    cookie_fill.add_argument("--target", required=True, type=int)
    cookie_fill.add_argument(
        "--pool",
        choices=["default", "overseas"],
        default="default",
        help="select the legacy-compatible Cookie Redis pool",
    )
    cookie_fill.add_argument("--confirm-external-write", action="store_true")
    cookie_maintain = sub.add_parser(
        "cookie-maintain",
        help="run the configured cookie target plan once",
    )
    cookie_maintain.add_argument("--confirm-external-write", action="store_true")
    legacy_import = sub.add_parser(
        "legacy-import",
        help="read pending legacy MySQL tasks into the V2 state store",
    )
    legacy_import.add_argument("--task", required=True, choices=sorted(LEGACY_TASKS))
    legacy_import.add_argument("--limit", type=int, default=100)
    legacy_export = sub.add_parser(
        "legacy-export",
        help="project one completed V2 job to a legacy result destination",
    )
    legacy_export.add_argument("job_id")
    legacy_export.add_argument("--task", required=True, choices=sorted(LEGACY_TASKS))
    legacy_export.add_argument("--target", required=True, choices=["redis", "mysql"])
    legacy_export.add_argument("--confirm-external-write", action="store_true")
    legacy_state = sub.add_parser(
        "legacy-sync-state",
        help="explicitly map one V2 job status back to one legacy task row",
    )
    legacy_state.add_argument("job_id")
    legacy_state.add_argument("--task", required=True, choices=sorted(LEGACY_TASKS))
    legacy_state.add_argument("--legacy-task-id", required=True)
    legacy_state.add_argument("--confirm-external-write", action="store_true")
    return parser


def _settings(db_override: Path | None) -> Settings:
    settings = Settings.from_env()
    if not db_override:
        return settings
    return replace(settings, db_path=db_override.resolve())


async def _run_workers_forever(app: Any) -> None:
    async with asyncio.TaskGroup() as group:
        group.create_task(app.worker.run_forever())
        if app.settings.delivery_worker_enabled:
            group.create_task(app.delivery_worker.run_forever())


def _create_job(app: Any, args: argparse.Namespace) -> tuple[dict[str, Any], bool]:
    structured_inputs = []
    for raw in args.input_json:
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValidationError("--input-json must contain valid JSON") from exc
        if not isinstance(value, dict):
            raise ValidationError("--input-json must contain one JSON object")
        structured_inputs.append(value)
    all_inputs = [*args.inputs, *structured_inputs]
    if not all_inputs:
        raise ValidationError(
            f"{args.command} requires positional inputs or --input-json"
        )
    return app.service.create_job(
        inputs=all_inputs,
        kind=args.kind,
        marketplace_id=args.marketplace,
        postal_code=args.postal_code,
        execution_mode=args.mode,
        priority=args.priority,
        max_attempts=args.max_attempts,
        idempotency_key=args.idempotency_key,
        options={"result_sinks": args.result_sink} if args.result_sink else None,
        external_result_write_authorized=args.confirm_external_result_write,
    )


def main() -> None:
    args = _parser().parse_args()
    try:
        settings = _settings(args.db)
        if args.command == "doctor":
            _print(configuration_report(settings))
            return
        if args.command == "run":
            preflight = configuration_report(settings)
            if not preflight["configuration_ready"]:
                preflight["ok"] = False
                preflight["error"] = {
                    "type": "MissingConfiguration",
                    "message": (
                        "run did not create a job; configure blocking_issues, "
                        "reload the environment, and run doctor again"
                    ),
                }
                _print(preflight)
                raise SystemExit(2)
        app = build_application(settings)
        if args.command == "init-db":
            _print({"ok": True, "db_path": str(settings.db_path)})
        elif args.command == "serve":
            from amazon_crawler.interfaces.api import create_app

            uvicorn.run(create_app(app), host=args.host, port=args.port)
        elif args.command == "worker":
            if args.once:
                count = asyncio.run(app.worker.run_until_idle(max_items=args.max_items))
                delivered = asyncio.run(
                    app.delivery_worker.run_until_idle(
                        max_deliveries=args.max_deliveries
                    )
                )
                _print(
                    {
                        "ok": True,
                        "processed": count,
                        "deliveries_processed": delivered,
                    }
                )
            else:
                asyncio.run(_run_workers_forever(app))
        elif args.command in {"create", "run"}:
            job, created = _create_job(app, args)
            if args.command == "run":
                report = asyncio.run(
                    run_scoped_job(
                        app,
                        job["id"],
                        timeout_seconds=args.timeout_seconds,
                    )
                )
                _print({"ok": report["completed"], "created": created, **report})
                if not report["completed"]:
                    raise SystemExit(2)
                return
            _print({"ok": True, "created": created, "job": job})
        elif args.command == "list":
            _print(
                {
                    "ok": True,
                    "jobs": app.store.list_jobs(limit=args.limit, status=args.status),
                }
            )
        elif args.command == "show":
            _print({"ok": True, "job": app.store.get_job(args.job_id)})
        elif args.command == "pause":
            _print({"ok": True, "job": app.store.request_pause(args.job_id)})
        elif args.command == "resume":
            _print({"ok": True, "job": app.store.resume(args.job_id)})
        elif args.command == "cancel":
            _print({"ok": True, "job": app.store.request_cancel(args.job_id)})
        elif args.command == "results":
            _print(
                {
                    "ok": True,
                    "results": app.store.list_results(args.job_id, limit=args.limit),
                }
            )
        elif args.command == "events":
            _print(
                {
                    "ok": True,
                    "events": app.store.list_events(args.job_id, limit=args.limit),
                }
            )
        elif args.command == "deliveries":
            _print(
                {
                    "ok": True,
                    "deliveries": app.store.list_deliveries(
                        args.job_id, limit=args.limit
                    ),
                }
            )
        elif args.command == "capabilities":
            _print({"ok": True, **app.service.capabilities()})
        elif args.command == "metrics":
            _print({"ok": True, "metrics": app.store.metrics()})
        elif args.command == "cookie-fill":
            if not args.confirm_external_write:
                raise ValidationError(
                    "cookie-fill requires --confirm-external-write because it sends external requests and writes Redis"
                )
            harvester = app.cookie_harvesters.get(args.pool)
            if harvester is None:
                raise ValidationError(f"cookie pool {args.pool!r} is not configured")
            postal_code = args.postal_code or marketplace_default_postal_code(
                args.marketplace
            )
            if postal_code is None:
                raise ValidationError(
                    "selected marketplace has no configured default delivery region"
                )
            report = asyncio.run(
                harvester.ensure_capacity(
                    args.marketplace,
                    postal_code,
                    args.target,
                )
            )
            _print(
                {
                    "ok": True,
                    "pool": args.pool,
                    "postal_selection": {
                        "postal_code": postal_code,
                        "source": "explicit"
                        if args.postal_code
                        else "marketplace_default",
                    },
                    "report": asdict(report),
                }
            )
        elif args.command == "cookie-maintain":
            if not args.confirm_external_write:
                raise ValidationError(
                    "cookie-maintain requires --confirm-external-write because it sends external requests and writes Redis"
                )
            if app.cookie_maintenance is None:
                raise ValidationError(
                    "cookie maintenance requires its enable flag, Redis URL, and target file"
                )
            reports = asyncio.run(app.cookie_maintenance.run_once())
            all_ok = all(bool(report.get("ok")) for report in reports)
            _print({"ok": all_ok, "reports": reports})
            if not all_ok:
                raise SystemExit(2)
        elif args.command == "legacy-import":
            if not settings.legacy_mysql_url:
                raise ValidationError("legacy-import requires CRAWLER_LEGACY_MYSQL_URL")
            gateway = SQLAlchemyLegacyGateway(settings.legacy_mysql_url)
            report = LegacyTaskImporter(gateway, app.service).import_pending(
                args.task,
                limit=args.limit,
            )
            _print({"ok": True, "report": asdict(report)})
        elif args.command == "legacy-export":
            if not args.confirm_external_write:
                raise ValidationError(
                    "legacy-export requires --confirm-external-write because it writes an external legacy destination"
                )
            job = app.store.get_job(args.job_id)
            assert_legacy_job_compatibility(args.task, job)
            results = app.store.list_results(args.job_id, limit=1000)
            totals: dict[str, int] = {}
            if args.target == "redis":
                if not settings.legacy_result_redis_url:
                    raise ValidationError(
                        "Redis export requires CRAWLER_LEGACY_RESULT_REDIS_URL"
                    )
                try:
                    import redis
                except ImportError as exc:
                    raise ValidationError(
                        "Redis export requires the redis package"
                    ) from exc
                client = redis.Redis.from_url(
                    settings.legacy_result_redis_url,
                    decode_responses=False,
                    socket_connect_timeout=5,
                    socket_timeout=10,
                )
                writer = LegacyRedisResultBuffer(client)
            else:
                if not settings.legacy_mysql_url:
                    raise ValidationError(
                        "MySQL export requires CRAWLER_LEGACY_MYSQL_URL"
                    )
                writer = LegacyMySQLResultWriter(
                    SQLAlchemyLegacyGateway(settings.legacy_mysql_url)
                )
            for result in results:
                counts = writer.publish(args.task, result)
                for key, value in counts.items():
                    totals[key] = totals.get(key, 0) + value
            _print(
                {
                    "ok": True,
                    "job_id": args.job_id,
                    "legacy_task": args.task,
                    "target": args.target,
                    "results_read": len(results),
                    "published": totals,
                    "delivery_semantics": "at-least-once; a crash after destination write requires reconciliation before retry",
                }
            )
        elif args.command == "legacy-sync-state":
            if not args.confirm_external_write:
                raise ValidationError(
                    "legacy-sync-state requires --confirm-external-write"
                )
            if not settings.legacy_mysql_url:
                raise ValidationError(
                    "legacy-sync-state requires CRAWLER_LEGACY_MYSQL_URL"
                )
            job = app.store.get_job(args.job_id)
            assert_legacy_job_compatibility(
                args.task,
                job,
                legacy_task_id=args.legacy_task_id,
            )
            state = legacy_state_for(
                job["status"],
                job.get("last_error_code"),
                args.task,
            )
            gateway = SQLAlchemyLegacyGateway(settings.legacy_mysql_url)
            gateway.update_task_state(args.task, args.legacy_task_id, state)
            _print(
                {
                    "ok": True,
                    "job_id": args.job_id,
                    "legacy_task": args.task,
                    "legacy_task_id": args.legacy_task_id,
                    "legacy_state": state,
                }
            )
    except (CrawlerError, RuntimeError, ValueError) as exc:
        _print({"ok": False, "error": _public_error(exc)})
        raise SystemExit(2) from exc
    except Exception as exc:
        _print(
            {
                "ok": False,
                "error": {
                    "type": exc.__class__.__name__,
                    "message": "internal operation failed",
                },
            }
        )
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
