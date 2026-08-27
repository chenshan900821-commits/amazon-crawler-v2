#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import tempfile
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from amazon_crawler.bootstrap import Application, build_application
from amazon_crawler.config import Settings
from amazon_crawler.domain.errors import CrawlerError
from amazon_crawler.domain.models import ClaimedItem, ExecutionMode
from amazon_crawler.infra.legacy_compat import (
    LEGACY_MAX_ATTEMPTS,
    legacy_state_for,
)
from amazon_crawler.plugins.amazon_collections import AmazonCollectionPlugin
from amazon_crawler.plugins.amazon_merchants import AmazonMerchantPlugin
from amazon_crawler.plugins.amazon_product import AmazonProductPlugin
from scripts.audit_archived_product_parity import (
    LegacyCollectionParser,
    LegacyMerchantParser,
    LegacyParser,
    _file_digest,
    _load_current_legacy_collection_parser,
    _load_current_legacy_merchant_parser,
    _load_current_legacy_parser,
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


PLAN_SCHEMA = "controlled-failure-dual-plan.v1"
PRODUCT_KINDS = {"product", "product_hw", "product_time"}
COLLECTION_KINDS = {
    "search",
    "search_hour",
    "reviews",
    "category_asin_list",
    "rank_list",
}
MERCHANT_KINDS = {"merchant", "merchant_home", "merchant_products"}
ALL_KINDS = PRODUCT_KINDS | COLLECTION_KINDS | MERCHANT_KINDS
FAILURE_CASES = {"no_result", "throttle"}
DIAGNOSTIC_SCHEMA = "controlled-failure-candidate-diagnostic.v1"
SAFE_DIAGNOSTIC_DETAIL_KEYS = {
    "error_type",
    "evidence",
    "http_status",
    "marketplace_id",
    "postal_code",
}
NO_RESULT_CODES = {
    "search": "no_results",
    "search_hour": "no_results",
    "product": "product_not_found",
    "product_hw": "product_not_found",
    "product_time": "product_not_found",
    "reviews": "no_reviews",
    "merchant": "merchant_has_no_products",
    "merchant_home": "merchant_has_no_products",
    "merchant_products": "merchant_page_has_no_products",
    "category_asin_list": "no_category_results",
    "rank_list": "no_rank_results",
}
THROTTLE_CODES = {
    "blocked_page",
    "upstream_blocked",
    "upstream_incomplete",
    "upstream_retryable",
}
SAFE_ERROR_TYPE_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_.]{0,95}")


def _validated_failure_case(
    *,
    kind: str,
    planned_case: str,
    failure: CrawlFailure,
    accept_observed_throttle: bool,
) -> str | None:
    if (
        planned_case == "no_result"
        and failure.code == NO_RESULT_CODES[kind]
        and not failure.retryable
    ):
        return "no_result"
    if failure.code in THROTTLE_CODES and failure.retryable:
        if planned_case == "throttle" or accept_observed_throttle:
            return "throttle"
    return None


KIND_TO_LEGACY_TASK = {
    "product": "product_jp",
    "product_hw": "product_hw_jp",
    "product_time": "product_time_jp",
    "search": "search_jp",
    "search_hour": "search_hour_jp",
    "reviews": "reviews",
    "category_asin_list": "asin_list_jp",
    "rank_list": "rank_list_jp",
    "merchant": "merchant",
    "merchant_home": "merchant_home",
    "merchant_products": "merchant_products",
}
EXECUTION_MODES = {
    "product_hw": ExecutionMode.OVERSEAS,
    "product_time": ExecutionMode.REALTIME,
}


class FailureCandidateMismatch(ControlledEvidenceError):
    """A safe, non-promotable observation for a candidate that missed its case."""

    def __init__(self, message: str, diagnostic: dict[str, Any]) -> None:
        super().__init__(message)
        self.diagnostic = diagnostic


def _candidate_diagnostic(
    *,
    kind: str,
    case: str,
    normalized_input: dict[str, Any],
    outcome: Any,
    responses: list[Any],
) -> dict[str, Any]:
    """Summarize a rejected live sample without retaining response or result values."""

    response_rows = []
    for response in responses:
        html = str(getattr(response, "html", ""))
        content_sha256 = getattr(response, "content_sha256", None)
        if not _is_sha256(content_sha256):
            content_sha256 = hashlib.sha256(html.encode("utf-8")).hexdigest()
        byte_count = getattr(response, "byte_count", None)
        if not isinstance(byte_count, int) or byte_count < 0:
            byte_count = len(html.encode("utf-8"))
        response_rows.append(
            {
                "status_code": int(getattr(response, "status_code", 0)),
                "content_sha256": content_sha256,
                "byte_count": byte_count,
            }
        )

    diagnostic: dict[str, Any] = {
        "kind": kind,
        "expected_case": case,
        "input_sha256": _digest(normalized_input),
        "response_count": len(response_rows),
        "responses": response_rows,
        "response_set_sha256": _digest(
            [row["content_sha256"] for row in response_rows]
        )
        if response_rows
        else None,
    }
    if outcome.ok and outcome.result is not None:
        data = outcome.result.data
        diagnostic["observed"] = {
            "status": "succeeded",
            "schema_version": outcome.result.schema_version,
            "data_keys": sorted(str(key) for key in data),
            "row_count": data.get("row_count")
            if isinstance(data.get("row_count"), int)
            else None,
            "item_count": len(data.get("items", []))
            if isinstance(data.get("items"), list)
            else None,
            "dimension_count": len(data.get("dimension_items", []))
            if isinstance(data.get("dimension_items"), list)
            else None,
            "followup_job_count": len(outcome.result.followup_jobs),
        }
    elif outcome.failure is not None:
        details = outcome.failure.details
        evidence = details.get("evidence") if isinstance(details, dict) else None
        response_sha256 = (
            evidence.get("sha256") if isinstance(evidence, dict) else None
        )
        error_type = details.get("error_type") if isinstance(details, dict) else None
        diagnostic["observed"] = {
            "status": "failed",
            "error_code": outcome.failure.code,
            "retryable": outcome.failure.retryable,
            "error_type": (
                error_type
                if isinstance(error_type, str)
                and SAFE_ERROR_TYPE_PATTERN.fullmatch(error_type)
                else None
            ),
            "http_status": details.get("http_status")
            if isinstance(details, dict)
            and isinstance(details.get("http_status"), int)
            else None,
            "response_sha256": response_sha256
            if _is_sha256(response_sha256)
            else None,
            "detail_keys": sorted(
                str(key) for key in details if key in SAFE_DIAGNOSTIC_DETAIL_KEYS
            )
            if isinstance(details, dict)
            else [],
        }
    else:
        diagnostic["observed"] = {"status": "invalid_plugin_outcome"}
    _scan_for_secret_material(diagnostic, path="candidate_diagnostic")
    return diagnostic


def _candidate_mismatch(
    message: str,
    *,
    kind: str,
    case: str,
    normalized_input: dict[str, Any],
    outcome: Any,
    recording: RecordingFetcher,
) -> FailureCandidateMismatch:
    return FailureCandidateMismatch(
        message,
        _candidate_diagnostic(
            kind=kind,
            case=case,
            normalized_input=normalized_input,
            outcome=outcome,
            responses=recording.responses,
        ),
    )


def _candidate_diagnostic_report(
    plan: dict[str, Any], mismatch: FailureCandidateMismatch
) -> dict[str, Any]:
    report = {
        "schema_version": DIAGNOSTIC_SCHEMA,
        "promotable": False,
        "reason": "expected_case_not_observed",
        "batch_id": plan["batch_id"],
        "run_id": plan["run_id"],
        "authorization_reference": plan["authorization_reference"],
        "environment": plan["environment"],
        "observed_at": datetime.now(UTC).isoformat(),
        "diagnostic": mismatch.diagnostic,
    }
    _scan_for_secret_material(report, path="candidate_diagnostic_report")
    return report


def validate_failure_dual_plan(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ControlledEvidenceError("failure dual plan must be an object")
    _scan_for_secret_material(value, path="plan")
    if value.get("schema_version") != PLAN_SCHEMA:
        raise ControlledEvidenceError(f"schema_version must be {PLAN_SCHEMA}")
    if value.get("environment") != "isolated":
        raise ControlledEvidenceError("failure dual plan environment must be isolated")
    for field in ("batch_id", "run_id", "authorization_reference"):
        if not isinstance(value.get(field), str) or not value[field].strip():
            raise ControlledEvidenceError(f"failure dual plan {field} is required")
    _validate_external_check_receipts(value)
    scenarios = value.get("scenarios")
    if not isinstance(scenarios, list) or not 1 <= len(scenarios) <= 22:
        raise ControlledEvidenceError(
            "failure dual plan requires between one and twenty-two scenarios"
        )
    identities: set[tuple[str, str]] = set()
    for index, scenario in enumerate(scenarios):
        if not isinstance(scenario, dict):
            raise ControlledEvidenceError(f"failure dual scenario {index} is invalid")
        kind = scenario.get("kind")
        case = scenario.get("case")
        identity = (str(kind), str(case))
        if kind not in ALL_KINDS or case not in FAILURE_CASES:
            raise ControlledEvidenceError("failure dual scenario kind or case is invalid")
        if identity in identities:
            raise ControlledEvidenceError(
                f"duplicate failure dual scenario {kind}/{case}"
            )
        identities.add(identity)
        if scenario.get("authorized") is not True:
            raise ControlledEvidenceError(f"scenario {kind}/{case} is not authorized")
        for field in ("marketplace_id", "postal_code"):
            if not isinstance(scenario.get(field), str) or not scenario[field].strip():
                raise ControlledEvidenceError(f"scenario {kind}/{case} needs {field}")
        if not isinstance(scenario.get("input"), dict) or not scenario["input"]:
            raise ControlledEvidenceError(f"scenario {kind}/{case} needs input")
        if "legacy" in scenario:
            raise ControlledEvidenceError(
                "failure dual plan must not carry a precomputed legacy result"
            )
    return value


def _legacy_source_hash(root: Path, kind: str) -> str:
    parser_files = {
        "product": "tools/product_parser_utils.py",
        "product_hw": "tools/product_parser_utils.py",
        "product_time": "tools/product_parser_utils.py",
        "search": "tools/search_parser_utils.py",
        "search_hour": "tools/search_hour_parser_utils.py",
        "reviews": "tools/reviews_parser_utils.py",
        "category_asin_list": "tools/asin_list_parser_utils.py",
        "rank_list": "tools/rank_list_parser_utils.py",
        "merchant": "tools/merchant_parser_utils.py",
        "merchant_home": "tools/merchant_home_parser_utils.py",
        "merchant_products": "tools/merchant_products_parser_utils.py",
    }
    return _digest(
        {
            "parser": _file_digest(root / parser_files[kind]),
            "helpers": _file_digest(root / "tools/lxml_tool.py"),
            "config": _file_digest(root / "settings/config.py"),
        }
    )


def _build_plugin(application: Application, kind: str, recording: RecordingFetcher) -> Any:
    template = application.plugins.get(kind)
    if kind in PRODUCT_KINDS:
        return AmazonProductPlugin(
            fetcher=recording,  # type: ignore[arg-type]
            evidence_store=template._evidence_store,  # type: ignore[attr-defined]
            require_cookie=template._require_cookie,  # type: ignore[attr-defined]
            kind=kind,
        )
    if kind in COLLECTION_KINDS:
        return AmazonCollectionPlugin(
            kind=kind,
            fetcher=recording,  # type: ignore[arg-type]
            evidence_store=template._evidence_store,  # type: ignore[attr-defined]
            require_cookie=template._require_cookie,  # type: ignore[attr-defined]
        )
    return AmazonMerchantPlugin(
        kind=kind,
        fetcher=recording,  # type: ignore[arg-type]
        evidence_store=template._evidence_store,  # type: ignore[attr-defined]
        require_cookie=template._require_cookie,  # type: ignore[attr-defined]
    )


def _legacy_failure_state(exc: Exception) -> int:
    name = type(exc).__name__
    if name == "FatalError":
        return -2
    if name == "NoPageError":
        return -3
    if name in {"OnePageError", "NoReviewError", "NoResultError"}:
        return -4
    return -1


def _safe_legacy_failure_reason(exc: Exception) -> str:
    message = str(exc)
    patterns = (
        ("验证码", "verification_page"),
        ("isEmpty", "empty_json_response"),
        ("页面数据不全", "incomplete_page"),
        ("请求被限制", "rate_limited"),
        ("商品数量异常", "missing_product_total"),
        ("页面数据格式错误", "invalid_stream_format"),
        ("响应码不是200", "non_success_status"),
        ("页面不存在", "page_not_found"),
        ("店铺无商品", "merchant_has_no_products"),
    )
    return next(
        (reason for marker, reason in patterns if marker in message),
        "unclassified_legacy_exception",
    )


def _execute_legacy(
    kind: str,
    responses: list[Any],
    task: dict[str, Any],
    *,
    product_parser: LegacyParser,
    collection_parser: LegacyCollectionParser,
    merchant_parser: LegacyMerchantParser,
) -> None:
    if kind in PRODUCT_KINDS:
        if len(responses) != 1:
            raise ControlledEvidenceError(
                "product failure replay requires exactly one upstream response"
            )
        response = responses[0]
        product_parser(response.html, task, int(response.status_code))
        return
    if kind in COLLECTION_KINDS:
        collection_parser(
            kind,
            [(response.html, int(response.status_code)) for response in responses],
            task,
        )
        return
    if len(responses) != 1:
        raise ControlledEvidenceError(
            "merchant failure replay requires exactly one upstream response"
        )
    response = responses[0]
    merchant_parser(
        kind,
        response.html,
        int(response.status_code),
        task,
        str(response.url),
    )


async def _execute_scenario(
    application: Application,
    scenario: dict[str, Any],
    *,
    product_parser: LegacyParser,
    collection_parser: LegacyCollectionParser,
    merchant_parser: LegacyMerchantParser,
    legacy_source_sha256: str,
    accept_observed_throttle: bool = False,
) -> dict[str, Any]:
    kind = str(scenario["kind"])
    case = str(scenario["case"])
    recording = RecordingFetcher(application.fetcher)
    plugin = _build_plugin(application, kind, recording)
    try:
        normalized = plugin.normalize(
            scenario["input"],
            str(scenario["marketplace_id"]),
            str(scenario["postal_code"]),
        )
    except (CrawlerError, TypeError, ValueError) as exc:
        raise ControlledEvidenceError(
            f"failure dual scenario {kind}/{case} input is invalid"
        ) from exc
    identity = _digest(
        {
            "run_id": scenario["run_id"],
            "kind": kind,
            "case": case,
            "input": normalized.as_dict(),
        }
    )[:24]
    legacy_task = KIND_TO_LEGACY_TASK[kind]
    item = ClaimedItem(
        id=f"controlled-item-{identity}",
        job_id=f"controlled-job-{identity}",
        seq=1,
        kind=kind,
        execution_mode=EXECUTION_MODES.get(kind, ExecutionMode.STANDARD),
        input=normalized.as_dict(),
        options={},
        attempts=1,
        max_attempts=LEGACY_MAX_ATTEMPTS.get(legacy_task, 5),
        lease_owner="controlled-failure-dual",
        lease_token=f"controlled-failure-dual-{identity}",
    )
    outcome = await plugin.execute(item)
    if outcome.ok or outcome.failure is None:
        raise _candidate_mismatch(
            f"failure dual scenario {kind}/{case} unexpectedly succeeded",
            kind=kind,
            case=case,
            normalized_input=normalized.as_dict(),
            outcome=outcome,
            recording=recording,
        )
    failure = outcome.failure
    observed_case = _validated_failure_case(
        kind=kind,
        planned_case=case,
        failure=failure,
        accept_observed_throttle=accept_observed_throttle,
    )
    if observed_case is None:
        raise _candidate_mismatch(
            f"failure dual scenario {kind}/{case} returned {failure.code}",
            kind=kind,
            case=case,
            normalized_input=normalized.as_dict(),
            outcome=outcome,
            recording=recording,
        )
    case = observed_case
    responses = recording.responses
    if not responses:
        raise ControlledEvidenceError(
            f"failure dual scenario {kind}/{case} did not receive an upstream response"
        )
    evidence = failure.details.get("evidence")
    response_sha256 = evidence.get("sha256") if isinstance(evidence, dict) else None
    if not _is_sha256(response_sha256):
        raise ControlledEvidenceError(
            f"failure dual scenario {kind}/{case} has no response SHA-256"
        )
    matching_response = any(
        response_sha256
        in {
            getattr(response, "content_sha256", None),
            hashlib.sha256(response.html.encode("utf-8")).hexdigest(),
        }
        for response in responses
    )
    if not matching_response:
        raise ControlledEvidenceError(
            f"failure dual scenario {kind}/{case} response evidence does not match"
        )
    try:
        _execute_legacy(
            kind,
            responses,
            normalized.as_dict(),
            product_parser=product_parser,
            collection_parser=collection_parser,
            merchant_parser=merchant_parser,
        )
    except ControlledEvidenceError:
        raise
    except Exception as exc:
        legacy_state = _legacy_failure_state(exc)
        legacy_error_type = type(exc).__name__
        legacy_error_reason = _safe_legacy_failure_reason(exc)
    else:
        raise ControlledEvidenceError(
            f"current legacy parser accepted failure scenario {kind}/{case}"
        )
    expected_state = legacy_state_for("failed", failure.code, legacy_task)
    if legacy_state != expected_state:
        raise ControlledEvidenceError(
            f"legacy failure state {legacy_state} ({legacy_error_reason}) "
            f"disagrees with V2 mapping {expected_state}"
        )
    response_set_sha256 = _digest(
        [hashlib.sha256(response.html.encode("utf-8")).hexdigest() for response in responses]
    )
    legacy_evidence = {
        "source": "current_legacy_source",
        "join_strategy": "same_response",
        "legacy_source_sha256": legacy_source_sha256,
        "response_sha256": str(response_sha256),
        "response_set_sha256": response_set_sha256,
        **(
            {
                "execution_adapter": (
                    "literal_eval_discard_debug_html"
                    if kind == "merchant_products"
                    else "literal_eval_only"
                )
            }
            if kind in {"search", "search_hour", "rank_list", "merchant_products"}
            else {}
        ),
    }
    return {
        "kind": kind,
        "case": case,
        "authorized": True,
        "marketplace_id": normalized.marketplace_id,
        "postal_code": normalized.postal_code,
        "input": normalized.as_dict(),
        "legacy": {
            "state": legacy_state,
            "projection": {
                "result_rows": [],
                "dimension_rows": [],
                "child_task_rows": [],
                "product_task_rows": [],
            },
            "evidence": legacy_evidence,
            "error_type": legacy_error_type,
        },
        "v2": {
            "status": "failed",
            "error_code": failure.code,
            "retryable": failure.retryable,
            "response_sha256": response_sha256,
            "response_set_sha256": response_set_sha256,
            "result": None,
        },
    }


async def collect_failure_dual_batch(
    plan: object,
    *,
    application: Application,
    product_parser: LegacyParser,
    collection_parser: LegacyCollectionParser,
    merchant_parser: LegacyMerchantParser,
    legacy_source_root: Path,
    expected_authorization_reference: str | None,
    accept_observed_throttle: bool = False,
) -> dict[str, Any]:
    plan = validate_failure_dual_plan(plan)
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
                product_parser=product_parser,
                collection_parser=collection_parser,
                merchant_parser=merchant_parser,
                legacy_source_sha256=_legacy_source_hash(
                    legacy_source_root, str(raw["kind"])
                ),
                accept_observed_throttle=accept_observed_throttle,
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
                "response_sets": [
                    scenario["v2"]["response_set_sha256"] for scenario in scenarios
                ],
            }
        ),
    }
    identities = {(scenario["kind"], scenario["case"]) for scenario in scenarios}
    missing = _missing_scenarios(identities)
    source_hashes = sorted(
        scenario["legacy"]["evidence"]["legacy_source_sha256"]
        for scenario in scenarios
    )
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
            "result_identity_sha256": _digest(
                [scenario["legacy"]["state"] for scenario in scenarios]
            ),
            "legacy_source_sha256": _digest(source_hashes),
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
            "Collect response-proven no-result or natural throttle outcomes and "
            "replay current legacy source against the same in-memory response set."
        )
    )
    parser.add_argument("plan", type=Path)
    parser.add_argument("--legacy-source-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--diagnostic-output",
        type=Path,
        help=(
            "write a non-promotable, hash-and-count-only report when the live "
            "candidate does not match its expected case"
        ),
    )
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--confirm-authorized-network", action="store_true")
    parser.add_argument(
        "--accept-observed-throttle",
        action="store_true",
        help=(
            "record a response-proven retryable throttle when an authorized "
            "no-result candidate naturally returns one"
        ),
    )
    args = parser.parse_args()
    project_root = Path(__file__).resolve().parents[1]
    try:
        plan = validate_failure_dual_plan(
            json.loads(args.plan.read_text(encoding="utf-8"))
        )
        if (
            args.diagnostic_output is not None
            and args.diagnostic_output.resolve() == args.output.resolve()
        ):
            raise ControlledEvidenceError(
                "diagnostic output must differ from the promotable batch output"
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
                "failure dual collection requires explicit network confirmation"
            )
        legacy_root = args.legacy_source_root.resolve()
        product_parser = _load_current_legacy_parser(legacy_root)
        collection_parser = _load_current_legacy_collection_parser(legacy_root)
        merchant_parser = _load_current_legacy_merchant_parser(legacy_root)
        with tempfile.TemporaryDirectory(prefix="amazon-crawler-failure-dual-") as tempdir:
            settings = replace(
                Settings.from_env(project_root),
                db_path=Path(tempdir) / "crawler.db",
                evidence_dir=Path(tempdir) / "evidence",
                capture_evidence=False,
                worker_enabled=False,
            )
            application = build_application(settings)
            bundle = asyncio.run(
                collect_failure_dual_batch(
                    plan,
                    application=application,
                    product_parser=product_parser,
                    collection_parser=collection_parser,
                    merchant_parser=merchant_parser,
                    legacy_source_root=legacy_root,
                    expected_authorization_reference=os.getenv(
                        "CONTROLLED_ACCEPTANCE_AUTHORIZATION"
                    ),
                    accept_observed_throttle=args.accept_observed_throttle,
                )
            )
        _atomic_write_outside_project(args.output, bundle, project_root=project_root)
        from scripts.merge_controlled_shadow_batches import batch_status

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
        diagnostic_output = None
        if isinstance(exc, FailureCandidateMismatch) and args.diagnostic_output:
            try:
                report = _candidate_diagnostic_report(plan, exc)
                _atomic_write_outside_project(
                    args.diagnostic_output,
                    report,
                    project_root=project_root,
                )
                diagnostic_output = str(args.diagnostic_output.resolve())
            except (OSError, ValueError, ControlledEvidenceError):
                print(
                    json.dumps(
                        {
                            "ok": False,
                            "error": {
                                "type": "ControlledEvidenceError",
                                "message": "candidate diagnostic could not be written safely",
                            },
                        },
                        ensure_ascii=False,
                    )
                )
                return 2
        message = (
            str(exc)
            if isinstance(exc, ControlledEvidenceError)
            else "failure dual shadow collection failed"
        )
        payload: dict[str, Any] = {
            "ok": False,
            "error": {"type": type(exc).__name__, "message": message},
        }
        if diagnostic_output is not None:
            payload["diagnostic_output"] = diagnostic_output
        print(
            json.dumps(
                payload,
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
