#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import tempfile
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from amazon_crawler.bootstrap import Application, build_application
from amazon_crawler.config import Settings
from amazon_crawler.domain.errors import CrawlerError
from amazon_crawler.domain.models import ClaimedItem, ExecutionMode
from amazon_crawler.infra.legacy_compat import LEGACY_MAX_ATTEMPTS
from amazon_crawler.plugins.amazon_collections import AmazonCollectionPlugin
from scripts.audit_archived_product_parity import (
    LegacyCollectionParser,
    _file_digest,
    _load_current_legacy_collection_parser,
)
from scripts.collect_controlled_v2_shadow import (
    BATCH_BUNDLE_SCHEMA,
    _atomic_write_outside_project,
    _digest,
    _missing_scenarios,
    _validate_external_check_receipts,
    validate_runtime_prerequisites,
)
from scripts.collect_product_dual_shadow import RecordingFetcher
from scripts.compile_controlled_evidence import (
    ControlledEvidenceError,
    _is_sha256,
    _scan_for_secret_material,
)
from scripts.runtime_source_fingerprint import runtime_source_sha256


PLAN_SCHEMA = "controlled-collection-dual-plan.v1"
COLLECTION_KINDS = {
    "search",
    "search_hour",
    "reviews",
    "category_asin_list",
    "rank_list",
}
LEGACY_TASKS = {
    "search": "search_jp",
    "search_hour": "search_hour_jp",
    "reviews": "reviews",
    "category_asin_list": "asin_list_jp",
    "rank_list": "rank_list_jp",
}


def _legacy_source_hash(legacy_source_root: Path) -> str:
    files = {
        "search": "tools/search_parser_utils.py",
        "search_hour": "tools/search_hour_parser_utils.py",
        "reviews": "tools/reviews_parser_utils.py",
        "category": "tools/asin_list_parser_utils.py",
        "rank": "tools/rank_list_parser_utils.py",
        "helpers": "tools/lxml_tool.py",
        "config": "settings/config.py",
    }
    return _digest(
        {name: _file_digest(legacy_source_root / path) for name, path in files.items()}
    )


def validate_collection_dual_plan(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ControlledEvidenceError("collection dual plan must be an object")
    _scan_for_secret_material(value, path="plan")
    if value.get("schema_version") != PLAN_SCHEMA:
        raise ControlledEvidenceError(f"schema_version must be {PLAN_SCHEMA}")
    if value.get("environment") != "isolated":
        raise ControlledEvidenceError("collection dual plan environment must be isolated")
    for field in ("batch_id", "run_id", "authorization_reference"):
        if not isinstance(value.get(field), str) or not value[field].strip():
            raise ControlledEvidenceError(f"collection dual plan {field} is required")
    _validate_external_check_receipts(value)
    scenarios = value.get("scenarios")
    if not isinstance(scenarios, list) or not 1 <= len(scenarios) <= len(
        COLLECTION_KINDS
    ):
        raise ControlledEvidenceError(
            "collection dual plan requires between one and five scenarios"
        )
    identities: set[tuple[str, str]] = set()
    for index, scenario in enumerate(scenarios):
        if not isinstance(scenario, dict):
            raise ControlledEvidenceError(f"collection dual scenario {index} is invalid")
        kind = scenario.get("kind")
        case = scenario.get("case")
        identity = (str(kind), str(case))
        if kind not in COLLECTION_KINDS or case != "success":
            raise ControlledEvidenceError(
                "collection dual plans currently accept only success scenarios"
            )
        if identity in identities:
            raise ControlledEvidenceError(
                f"duplicate collection dual scenario {kind}/{case}"
            )
        identities.add(identity)
        if scenario.get("authorized") is not True:
            raise ControlledEvidenceError(f"scenario {kind}/{case} is not authorized")
        for field in ("marketplace_id", "postal_code"):
            if not isinstance(scenario.get(field), str) or not scenario[field].strip():
                raise ControlledEvidenceError(
                    f"scenario {kind}/{case} needs {field}"
                )
        if not isinstance(scenario.get("input"), dict) or not scenario["input"]:
            raise ControlledEvidenceError(f"scenario {kind}/{case} needs input")
        if "legacy" in scenario:
            raise ControlledEvidenceError(
                "collection dual plan must not carry a precomputed legacy result"
            )
    return value


async def _execute_scenario(
    application: Application,
    scenario: dict[str, Any],
    *,
    legacy_parser: LegacyCollectionParser,
    legacy_source_sha256: str,
) -> dict[str, Any]:
    kind = str(scenario["kind"])
    template = application.plugins.get(kind)
    recording = RecordingFetcher(application.fetcher)
    plugin = AmazonCollectionPlugin(
        kind=kind,
        fetcher=recording,  # type: ignore[arg-type]
        evidence_store=template._evidence_store,  # type: ignore[attr-defined]
        require_cookie=template._require_cookie,  # type: ignore[attr-defined]
    )
    try:
        normalized = plugin.normalize(
            scenario["input"],
            str(scenario["marketplace_id"]),
            str(scenario["postal_code"]),
        )
    except (CrawlerError, TypeError, ValueError) as exc:
        raise ControlledEvidenceError(
            f"collection dual scenario {kind}/success input is invalid"
        ) from exc
    identity = _digest(
        {
            "run_id": scenario["run_id"],
            "kind": kind,
            "case": "success",
            "input": normalized.as_dict(),
        }
    )[:24]
    item = ClaimedItem(
        id=f"controlled-item-{identity}",
        job_id=f"controlled-job-{identity}",
        seq=1,
        kind=kind,
        execution_mode=ExecutionMode.STANDARD,
        input=normalized.as_dict(),
        options={},
        attempts=1,
        max_attempts=LEGACY_MAX_ATTEMPTS.get(LEGACY_TASKS[kind], 5),
        lease_owner="controlled-collection-dual",
        lease_token=f"controlled-collection-dual-{identity}",
    )
    outcome = await plugin.execute(item)
    if not outcome.ok or outcome.result is None:
        code = outcome.failure.code if outcome.failure is not None else "unknown"
        raise ControlledEvidenceError(
            f"collection dual scenario {kind}/success did not succeed: {code}"
        )
    if not recording.responses:
        raise ControlledEvidenceError(
            f"collection dual scenario {kind}/success captured no response"
        )
    response_hashes = [
        hashlib.sha256(response.html.encode("utf-8")).hexdigest()
        for response in recording.responses
    ]
    response_sha256 = outcome.result.evidence.get("sha256")
    if not _is_sha256(response_sha256) or response_sha256 != response_hashes[0]:
        raise ControlledEvidenceError(
            f"collection dual scenario {kind}/success response evidence does not match"
        )
    try:
        legacy_rows = legacy_parser(
            kind,
            [
                (response.html, int(response.status_code))
                for response in recording.responses
            ],
            normalized.as_dict(),
        )
    except Exception as exc:
        raise ControlledEvidenceError(
            f"current legacy parser rejected collection scenario {kind}/success "
            f"with {type(exc).__name__}"
        ) from exc
    if not isinstance(legacy_rows, list) or not all(
        isinstance(row, dict) for row in legacy_rows
    ):
        raise ControlledEvidenceError("current legacy collection parser returned invalid rows")
    response_set_sha256 = _digest(response_hashes)
    legacy_evidence = {
        "source": "current_legacy_source",
        "join_strategy": "same_response",
        "legacy_source_sha256": legacy_source_sha256,
        "response_sha256": str(response_sha256),
        "response_set_sha256": response_set_sha256,
        "execution_adapter": "literal_eval_only",
    }
    result_evidence = dict(outcome.result.evidence)
    result_evidence["response_set_sha256"] = response_set_sha256
    return {
        "kind": kind,
        "case": "success",
        "authorized": True,
        "marketplace_id": normalized.marketplace_id,
        "postal_code": normalized.postal_code,
        "input": normalized.as_dict(),
        "legacy": {
            "state": 1,
            "projection": {
                "result_rows": legacy_rows,
                "dimension_rows": [],
                "child_task_rows": [],
                "product_task_rows": [],
            },
            "evidence": legacy_evidence,
        },
        "v2": {
            "status": "succeeded",
            "error_code": None,
            "retryable": False,
            "response_sha256": response_sha256,
            "result": {
                "schema_version": outcome.result.schema_version,
                "data": outcome.result.data,
                "evidence": result_evidence,
            },
        },
    }


async def collect_collection_dual_batch(
    plan: object,
    *,
    application: Application,
    legacy_parser: LegacyCollectionParser,
    legacy_source_sha256: str,
    expected_authorization_reference: str | None,
) -> dict[str, Any]:
    plan = validate_collection_dual_plan(plan)
    validate_runtime_prerequisites(
        application=application,
        authorization_reference=plan["authorization_reference"],
        expected_authorization_reference=expected_authorization_reference,
    )
    scenarios = []
    for raw in plan["scenarios"]:
        scenarios.append(
            await _execute_scenario(
                application,
                {**raw, "run_id": plan["run_id"]},
                legacy_parser=legacy_parser,
                legacy_source_sha256=legacy_source_sha256,
            )
        )
    validated_at = datetime.now(UTC).isoformat()
    check_receipts = _validate_external_check_receipts(plan)
    runtime = {
        "transport_backend": application.fetcher.backend,
        "tls_impersonation": application.fetcher.tls_impersonation,
        "v2_source_sha256": runtime_source_sha256(
            Path(__file__).resolve().parents[1]
        ),
    }
    all_response_sets = [
        scenario["legacy"]["evidence"]["response_set_sha256"]
        for scenario in scenarios
    ]
    check_receipts["tls_impersonation"] = {
        "schema_version": "controlled-check-receipt.v1",
        "check": "tls_impersonation",
        "status": "passed",
        "environment": "isolated",
        "run_id": plan["run_id"],
        "authorization_reference": plan["authorization_reference"],
        "validated_at": validated_at,
        "artifact_sha256": _digest(
            {
                "runtime": runtime,
                "response_sets": all_response_sets,
            }
        ),
    }
    identities = {(scenario["kind"], scenario["case"]) for scenario in scenarios}
    missing = _missing_scenarios(identities)
    projections = [scenario["legacy"]["projection"] for scenario in scenarios]
    bundle = {
        "schema_version": BATCH_BUNDLE_SCHEMA,
        "batch_id": plan["batch_id"],
        "run_id": plan["run_id"],
        "authorization_reference": plan["authorization_reference"],
        "validated_at": validated_at,
        "environment": "isolated",
        "runtime": runtime,
        "source_evidence": {
            "source": "current_legacy_source",
            "join_strategy": "same_response",
            "task_identity_sha256": _digest(
                [scenario["input"] for scenario in scenarios]
            ),
            "result_identity_sha256": _digest(projections),
            "legacy_source_sha256": legacy_source_sha256,
        },
        "check_receipts": check_receipts,
        "scenarios": scenarios,
        "matrix_complete": not missing,
        "missing_scenarios": missing,
    }
    _scan_for_secret_material(bundle, path="bundle")
    return bundle


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Fetch authorized collection pages once and compare current legacy "
            "source with V2 against the same in-memory response set."
        )
    )
    parser.add_argument("plan", type=Path)
    parser.add_argument("--legacy-source-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--confirm-authorized-network", action="store_true")
    args = parser.parse_args()
    project_root = Path(__file__).resolve().parents[1]
    try:
        plan = validate_collection_dual_plan(
            json.loads(args.plan.read_text(encoding="utf-8"))
        )
        if args.validate_only:
            print(
                json.dumps(
                    {
                        "ok": True,
                        "validated_only": True,
                        "scenario_count": len(plan["scenarios"]),
                        "network_requests": 0,
                    },
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        if not args.confirm_authorized_network:
            raise ControlledEvidenceError(
                "collection dual collection requires explicit network confirmation"
            )
        legacy_source_root = args.legacy_source_root.resolve()
        legacy_parser = _load_current_legacy_collection_parser(legacy_source_root)
        legacy_source_sha256 = _legacy_source_hash(legacy_source_root)
        with tempfile.TemporaryDirectory(prefix="amazon-crawler-collection-dual-") as tempdir:
            settings = replace(
                Settings.from_env(project_root),
                db_path=Path(tempdir) / "crawler.db",
                evidence_dir=Path(tempdir) / "evidence",
                capture_evidence=False,
                worker_enabled=False,
            )
            application = build_application(settings)
            bundle = asyncio.run(
                collect_collection_dual_batch(
                    plan,
                    application=application,
                    legacy_parser=legacy_parser,
                    legacy_source_sha256=legacy_source_sha256,
                    expected_authorization_reference=os.getenv(
                        "CONTROLLED_ACCEPTANCE_AUTHORIZATION"
                    ),
                )
            )
        _atomic_write_outside_project(
            args.output,
            bundle,
            project_root=project_root,
        )
        from scripts.merge_controlled_shadow_batches import batch_status

        # Re-read the persisted artifact so the CLI summary proves the exact
        # serialized evidence that downstream merge and promotion will see.
        persisted = json.loads(args.output.read_text(encoding="utf-8"))
        status = batch_status([persisted])
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        ControlledEvidenceError,
        RuntimeError,
        ValueError,
    ) as exc:
        message = (
            str(exc)
            if isinstance(exc, ControlledEvidenceError)
            else "collection dual shadow collection failed"
        )
        print(
            json.dumps(
                {"ok": False, "error": {"type": type(exc).__name__, "message": message}},
                ensure_ascii=False,
            )
        )
        return 2
    print(
        json.dumps(
            {
                "ok": status["failed_scenarios"] == 0,
                "output": str(args.output.resolve()),
                "scenario_count": status["scenario_count"],
                "passed_scenarios": status["passed_scenarios"],
                "failed_scenarios": status["failed_scenarios"],
                "same_response_proof": True,
                "matrix_complete": status["matrix_complete"],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if status["failed_scenarios"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
