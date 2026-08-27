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
from scripts.build_controlled_run_report import (
    KIND_TO_LEGACY_TASK,
    _projection_payload,
)
from scripts.compile_controlled_evidence import (
    REQUIRED_GLOBAL_CHECKS,
    SCENARIO_CASES,
    ControlledEvidenceError,
    _is_sha256,
    _scan_for_secret_material,
    _validate_timestamp,
)
from scripts.verify_parity_manifest import EXPECTED_TASKS
from scripts.runtime_source_fingerprint import runtime_source_sha256


EXTERNALLY_PROVEN_CHECKS = REQUIRED_GLOBAL_CHECKS - {"tls_impersonation"}
TERMINAL_MODES = {
    "product_hw": ExecutionMode.OVERSEAS,
    "product_time": ExecutionMode.REALTIME,
}
FINAL_PLAN_SCHEMA = "controlled-shadow-plan.v1"
BATCH_PLAN_SCHEMA = "controlled-shadow-batch-plan.v1"
FINAL_BUNDLE_SCHEMA = "controlled-shadow-bundle.v1"
BATCH_BUNDLE_SCHEMA = "controlled-shadow-batch.v1"


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _validate_source_evidence(
    value: object,
    *,
    path: str = "source_evidence",
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ControlledEvidenceError(f"{path} must be an object")
    base_fields = {
        "source",
        "join_strategy",
        "task_identity_sha256",
        "result_identity_sha256",
    }
    allowed_fields = base_fields | {"legacy_source_sha256"}
    if not base_fields.issubset(value) or not set(value).issubset(allowed_fields):
        raise ControlledEvidenceError(f"{path} fields are invalid")
    source = value.get("source")
    join_strategy = value.get("join_strategy")
    if source == "legacy_loopback_mysql":
        if join_strategy not in {"task_id", "asin_market_postal_latest"}:
            raise ControlledEvidenceError(f"{path} join strategy is invalid")
        if "legacy_source_sha256" in value:
            raise ControlledEvidenceError(
                f"{path} legacy source hash is not valid for a MySQL observation"
            )
    elif source == "current_legacy_source":
        if join_strategy != "same_response":
            raise ControlledEvidenceError(f"{path} must use same_response")
        if not _is_sha256(value.get("legacy_source_sha256")):
            raise ControlledEvidenceError(f"{path} legacy source hash is invalid")
    else:
        raise ControlledEvidenceError(f"{path} source is invalid")
    for field in ("task_identity_sha256", "result_identity_sha256"):
        if not _is_sha256(value.get(field)):
            raise ControlledEvidenceError(f"{path} {field} is invalid")
    return dict(value)


def _validate_external_check_receipts(
    plan: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    receipts = plan.get("check_receipts")
    if not isinstance(receipts, dict) or set(receipts) != EXTERNALLY_PROVEN_CHECKS:
        raise ControlledEvidenceError(
            "shadow plan requires checkpoint, bridge, and redaction check receipts"
        )
    validated: dict[str, dict[str, Any]] = {}
    for check in sorted(EXTERNALLY_PROVEN_CHECKS):
        receipt = receipts[check]
        if not isinstance(receipt, dict):
            raise ControlledEvidenceError(f"check receipt {check} must be an object")
        if (
            receipt.get("schema_version") != "controlled-check-receipt.v1"
            or receipt.get("check") != check
            or receipt.get("status") != "passed"
            or receipt.get("environment") != "isolated"
            or receipt.get("run_id") != plan["run_id"]
            or receipt.get("authorization_reference")
            != plan["authorization_reference"]
            or not _is_sha256(receipt.get("artifact_sha256"))
        ):
            raise ControlledEvidenceError(f"check receipt {check} is invalid")
        _validate_timestamp(receipt.get("validated_at"))
        validated[check] = dict(receipt)
    return validated


def _validate_scenarios(
    plan: dict[str, Any],
    *,
    require_complete: bool,
) -> set[tuple[str, str]]:
    scenarios = plan.get("scenarios")
    expected_count = len(EXPECTED_TASKS) * len(SCENARIO_CASES)
    if not isinstance(scenarios, list):
        raise ControlledEvidenceError("controlled shadow scenarios must be a list")
    if require_complete and len(scenarios) != expected_count:
        raise ControlledEvidenceError(
            f"controlled shadow plan requires exactly {expected_count} scenarios"
        )
    if not require_complete and not 1 <= len(scenarios) <= expected_count:
        raise ControlledEvidenceError(
            f"controlled shadow batch requires between 1 and {expected_count} scenarios"
        )

    identities: set[tuple[str, str]] = set()
    marketplaces: set[str] = set()
    postal_codes: set[str] = set()
    for index, scenario in enumerate(scenarios):
        if not isinstance(scenario, dict):
            raise ControlledEvidenceError(f"scenario {index} must be an object")
        kind = scenario.get("kind")
        case = scenario.get("case")
        if kind not in EXPECTED_TASKS or case not in SCENARIO_CASES:
            raise ControlledEvidenceError(f"scenario {index} kind or case is invalid")
        identity = (str(kind), str(case))
        if identity in identities:
            raise ControlledEvidenceError(f"duplicate scenario {kind}/{case}")
        identities.add(identity)
        if scenario.get("authorized") is not True:
            raise ControlledEvidenceError(f"scenario {kind}/{case} is not authorized")
        marketplace = scenario.get("marketplace_id")
        postal_code = scenario.get("postal_code")
        if not isinstance(marketplace, str) or not marketplace.strip():
            raise ControlledEvidenceError(f"scenario {kind}/{case} needs marketplace_id")
        if not isinstance(postal_code, str) or not postal_code.strip():
            raise ControlledEvidenceError(f"scenario {kind}/{case} needs postal_code")
        if not isinstance(scenario.get("input"), dict) or not scenario["input"]:
            raise ControlledEvidenceError(f"scenario {kind}/{case} needs an input object")
        legacy = scenario.get("legacy")
        if not isinstance(legacy, dict):
            raise ControlledEvidenceError(f"scenario {kind}/{case} needs legacy observation")
        state = legacy.get("state")
        if isinstance(state, bool) or not isinstance(state, int):
            raise ControlledEvidenceError(f"scenario {kind}/{case} needs legacy state")
        _projection_payload(
            legacy.get("projection"),
            path=f"scenario {kind}/{case}.legacy.projection",
        )
        marketplaces.add(marketplace.upper())
        postal_codes.add(postal_code)

    if require_complete:
        expected = {
            (kind, case) for kind in EXPECTED_TASKS for case in SCENARIO_CASES
        }
        if identities != expected:
            raise ControlledEvidenceError("controlled shadow scenario matrix is incomplete")
        if not {"US", "JP"}.issubset(marketplaces):
            raise ControlledEvidenceError("controlled shadow plan must cover US and JP")
        if len(postal_codes) < 2:
            raise ControlledEvidenceError(
                "controlled shadow plan must cover at least two postal codes"
            )
    return identities


def _validate_shadow_document(
    plan: object,
    *,
    batch: bool,
) -> dict[str, Any]:
    if not isinstance(plan, dict):
        raise ControlledEvidenceError("controlled shadow plan must be an object")
    _scan_for_secret_material(plan, path="plan")
    expected_schema = BATCH_PLAN_SCHEMA if batch else FINAL_PLAN_SCHEMA
    if plan.get("schema_version") != expected_schema:
        raise ControlledEvidenceError(
            f"schema_version must be {expected_schema}"
        )
    if plan.get("environment") != "isolated":
        raise ControlledEvidenceError("controlled shadow environment must be isolated")
    for field in ("run_id", "authorization_reference"):
        if not isinstance(plan.get(field), str) or not plan[field].strip():
            raise ControlledEvidenceError(f"{field} is required")
    if batch and (
        not isinstance(plan.get("batch_id"), str) or not plan["batch_id"].strip()
    ):
        raise ControlledEvidenceError("batch_id is required")
    if batch and plan.get("source_evidence") is not None:
        _validate_source_evidence(plan["source_evidence"])
    _validate_external_check_receipts(plan)
    _validate_scenarios(plan, require_complete=not batch)
    return plan


def validate_shadow_plan(plan: object) -> dict[str, Any]:
    return _validate_shadow_document(plan, batch=False)


def validate_shadow_batch_plan(plan: object) -> dict[str, Any]:
    return _validate_shadow_document(plan, batch=True)


def validate_runtime_prerequisites(
    *,
    application: Application,
    authorization_reference: str,
    expected_authorization_reference: str | None,
) -> None:
    if expected_authorization_reference != authorization_reference:
        raise ControlledEvidenceError(
            "CONTROLLED_ACCEPTANCE_AUTHORIZATION must match the plan authorization reference"
        )
    settings = application.settings
    missing = []
    if not settings.require_cookie:
        missing.append("required-cookie enforcement")
    if not settings.cookie_redis_url:
        missing.append("isolated default Cookie Redis")
    if not settings.cookie_redis_overseas_url:
        missing.append("isolated overseas Cookie Redis")
    if not settings.merchant_cookie:
        missing.append("isolated merchant/rank Cookie")
    if not settings.proxy_extract_url:
        missing.append("isolated dynamic proxy source")
    if missing:
        raise ControlledEvidenceError(
            "controlled shadow runtime is missing required isolated resources: "
            + ", ".join(missing)
        )
    if application.fetcher.backend != "curl_cffi" or not application.fetcher.tls_impersonation:
        raise ControlledEvidenceError(
            "controlled shadow runtime must use actual curl_cffi TLS impersonation"
        )


def _response_hash_from_failure(details: dict[str, Any]) -> str | None:
    evidence = details.get("evidence")
    if not isinstance(evidence, dict):
        return None
    value = evidence.get("sha256")
    return str(value) if _is_sha256(value) else None


async def _execute_scenario(
    application: Application,
    scenario: dict[str, Any],
) -> dict[str, Any]:
    kind = str(scenario["kind"])
    case = str(scenario["case"])
    plugin = application.plugins.get(kind)
    try:
        normalized = plugin.normalize(
            scenario["input"],
            str(scenario["marketplace_id"]),
            str(scenario["postal_code"]),
        )
    except (CrawlerError, TypeError, ValueError) as exc:
        raise ControlledEvidenceError(
            f"scenario {kind}/{case} input does not satisfy its V2 contract"
        ) from exc
    identity = _digest(
        {"run_id": scenario.get("run_id"), "kind": kind, "case": case}
    )[:24]
    legacy_task = KIND_TO_LEGACY_TASK[kind]
    item = ClaimedItem(
        id=f"controlled-item-{identity}",
        job_id=f"controlled-job-{identity}",
        seq=1,
        kind=kind,
        execution_mode=TERMINAL_MODES.get(kind, ExecutionMode.STANDARD),
        input=normalized.as_dict(),
        options={},
        attempts=1,
        max_attempts=LEGACY_MAX_ATTEMPTS.get(legacy_task, 5),
        lease_owner="controlled-shadow",
        lease_token=f"controlled-shadow-{identity}",
    )
    try:
        outcome = await plugin.execute(item)
    except Exception as exc:
        raise ControlledEvidenceError(
            f"scenario {kind}/{case} raised an unexpected {type(exc).__name__}"
        ) from exc

    if outcome.ok and outcome.result is not None:
        response_sha = outcome.result.evidence.get("sha256")
        if not _is_sha256(response_sha):
            raise ControlledEvidenceError(
                f"scenario {kind}/{case} success has no response SHA-256"
            )
        v2_observation: dict[str, Any] = {
            "status": "succeeded",
            "error_code": None,
            "retryable": False,
            "response_sha256": response_sha,
            "result": {
                "schema_version": outcome.result.schema_version,
                "data": outcome.result.data,
                "evidence": outcome.result.evidence,
            },
        }
    elif outcome.failure is not None:
        response_sha = _response_hash_from_failure(outcome.failure.details)
        if response_sha is None:
            raise ControlledEvidenceError(
                f"scenario {kind}/{case} did not receive an upstream response with evidence"
            )
        v2_observation = {
            "status": "failed",
            "error_code": outcome.failure.code,
            "retryable": outcome.failure.retryable,
            "response_sha256": response_sha,
            "result": None,
        }
    else:
        raise ControlledEvidenceError(
            f"scenario {kind}/{case} returned no result or failure"
        )
    return {
        "kind": kind,
        "case": case,
        "authorized": True,
        "marketplace_id": normalized.marketplace_id,
        "postal_code": normalized.postal_code,
        "input": normalized.as_dict(),
        "legacy": scenario["legacy"],
        "v2": v2_observation,
    }


def _missing_scenarios(identities: set[tuple[str, str]]) -> list[str]:
    expected = {
        (kind, case) for kind in EXPECTED_TASKS for case in SCENARIO_CASES
    }
    return [f"{kind}/{case}" for kind, case in sorted(expected - identities)]


async def _collect_shadow_artifact(
    plan: object,
    *,
    application: Application,
    expected_authorization_reference: str | None,
    concurrency: int = 2,
    batch: bool,
) -> dict[str, Any]:
    plan = (
        validate_shadow_batch_plan(plan)
        if batch
        else validate_shadow_plan(plan)
    )
    validate_runtime_prerequisites(
        application=application,
        authorization_reference=plan["authorization_reference"],
        expected_authorization_reference=expected_authorization_reference,
    )
    semaphore = asyncio.Semaphore(max(1, min(int(concurrency), 8)))

    async def execute(index: int, raw: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        scenario = {**raw, "run_id": plan["run_id"]}
        async with semaphore:
            return index, await _execute_scenario(application, scenario)

    collected = await asyncio.gather(
        *(execute(index, scenario) for index, scenario in enumerate(plan["scenarios"]))
    )
    scenarios = [value for _, value in sorted(collected)]
    validated_at = datetime.now(UTC).isoformat()
    response_hashes = [scenario["v2"]["response_sha256"] for scenario in scenarios]
    check_receipts = _validate_external_check_receipts(plan)
    runtime = {
        "transport_backend": application.fetcher.backend,
        "tls_impersonation": application.fetcher.tls_impersonation,
        "v2_source_sha256": runtime_source_sha256(
            Path(__file__).resolve().parents[1]
        ),
    }
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
                "response_hashes": response_hashes,
            }
        ),
    }
    identities = {
        (str(scenario["kind"]), str(scenario["case"])) for scenario in scenarios
    }
    bundle = {
        "schema_version": BATCH_BUNDLE_SCHEMA if batch else FINAL_BUNDLE_SCHEMA,
        "run_id": plan["run_id"],
        "authorization_reference": plan["authorization_reference"],
        "validated_at": validated_at,
        "environment": "isolated",
        "runtime": runtime,
        "check_receipts": check_receipts,
        "scenarios": scenarios,
    }
    if batch:
        missing_scenarios = _missing_scenarios(identities)
        bundle.update(
            {
                "batch_id": plan["batch_id"],
                "matrix_complete": not missing_scenarios,
                "missing_scenarios": missing_scenarios,
            }
        )
        if plan.get("source_evidence") is not None:
            bundle["source_evidence"] = dict(plan["source_evidence"])
    _scan_for_secret_material(bundle, path="bundle")
    return bundle


async def collect_shadow_bundle(
    plan: object,
    *,
    application: Application,
    expected_authorization_reference: str | None,
    concurrency: int = 2,
) -> dict[str, Any]:
    return await _collect_shadow_artifact(
        plan,
        application=application,
        expected_authorization_reference=expected_authorization_reference,
        concurrency=concurrency,
        batch=False,
    )


async def collect_shadow_batch(
    plan: object,
    *,
    application: Application,
    expected_authorization_reference: str | None,
    concurrency: int = 2,
) -> dict[str, Any]:
    return await _collect_shadow_artifact(
        plan,
        application=application,
        expected_authorization_reference=expected_authorization_reference,
        concurrency=concurrency,
        batch=True,
    )


def _atomic_write_outside_project(
    path: Path,
    payload: object,
    *,
    project_root: Path,
) -> None:
    if path.is_symlink():
        raise ControlledEvidenceError("refusing to replace a symlinked shadow bundle")
    path = path.resolve()
    if path.is_relative_to(project_root.resolve()):
        raise ControlledEvidenceError(
            "raw controlled shadow bundles must be written outside the project"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(_canonical_bytes(payload) + b"\n")
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Execute all authorized V2 shadow scenarios with the real runtime. "
            "This sends Amazon requests and uses isolated Cookie/proxy resources."
        )
    )
    parser.add_argument("plan", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="validate the plan without loading runtime resources or sending requests",
    )
    parser.add_argument("--confirm-authorized-network", action="store_true")
    args = parser.parse_args()
    project_root = Path(__file__).resolve().parents[1]
    try:
        plan = json.loads(args.plan.read_text(encoding="utf-8"))
        is_batch = plan.get("schema_version") == BATCH_PLAN_SCHEMA if isinstance(plan, dict) else False
        validated_plan = (
            validate_shadow_batch_plan(plan) if is_batch else validate_shadow_plan(plan)
        )
        if args.validate_only:
            identities = {
                (str(value["kind"]), str(value["case"]))
                for value in validated_plan["scenarios"]
            }
            missing_scenarios = _missing_scenarios(identities)
            print(
                json.dumps(
                    {
                        "ok": True,
                        "validated_only": True,
                        "artifact_type": "batch" if is_batch else "final",
                        "scenario_count": len(validated_plan["scenarios"]),
                        "matrix_complete": not missing_scenarios,
                        "missing_scenarios": missing_scenarios,
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
                "controlled shadow collection requires explicit network confirmation"
            )
        if args.output is None:
            raise ControlledEvidenceError(
                "controlled shadow collection requires --output"
            )
        with tempfile.TemporaryDirectory(prefix="amazon-crawler-controlled-") as tempdir:
            settings = replace(
                Settings.from_env(project_root),
                db_path=Path(tempdir) / "crawler.db",
                evidence_dir=Path(tempdir) / "evidence",
                capture_evidence=False,
                worker_enabled=False,
            )
            application = build_application(settings)
            bundle = asyncio.run(
                _collect_shadow_artifact(
                    plan,
                    application=application,
                    expected_authorization_reference=os.getenv(
                        "CONTROLLED_ACCEPTANCE_AUTHORIZATION"
                    ),
                    concurrency=args.concurrency,
                    batch=is_batch,
                )
            )
        _atomic_write_outside_project(
            args.output,
            bundle,
            project_root=project_root,
        )
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
            else "controlled V2 shadow collection failed"
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
                "ok": True,
                "output": str(args.output.resolve()),
                "artifact_type": "batch" if is_batch else "final",
                "scenario_count": len(bundle["scenarios"]),
                "matrix_complete": bool(bundle.get("matrix_complete", True)),
                "raw_bundle_in_project": False,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
