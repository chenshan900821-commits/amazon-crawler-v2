from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from amazon_crawler.domain.errors import CrawlerError
from amazon_crawler.infra.legacy_compat import (
    LEGACY_TASKS,
    legacy_state_for,
    project_legacy_result,
)
from amazon_crawler.plugins.amazon_collections import AmazonCollectionPlugin
from amazon_crawler.plugins.amazon_merchants import AmazonMerchantPlugin
from amazon_crawler.plugins.amazon_product import AmazonProductPlugin
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


PROJECTION_KEYS = {
    "result_rows",
    "dimension_rows",
    "child_task_rows",
    "product_task_rows",
}
VOLATILE_TIMESTAMP_FIELDS = {"created_at", "updated_at", "crawl_date"}
KIND_TO_LEGACY_TASK = {
    v2_kind: task_name
    for task_name, (_, _, v2_kind) in LEGACY_TASKS.items()
}


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _projection_payload(value: object, *, path: str) -> dict[str, list[object]]:
    if not isinstance(value, dict):
        raise ControlledEvidenceError(f"{path} must be an object")
    unexpected = sorted(set(value) - PROJECTION_KEYS)
    missing = sorted(PROJECTION_KEYS - set(value))
    if unexpected or missing:
        raise ControlledEvidenceError(
            f"{path} must contain exactly the four legacy projection lists"
        )
    result: dict[str, list[object]] = {}
    for key in sorted(PROJECTION_KEYS):
        rows = value[key]
        if not isinstance(rows, list) or not all(
            isinstance(row, dict) for row in rows
        ):
            raise ControlledEvidenceError(f"{path}.{key} must be a list of objects")
        result[key] = rows
    return result


def _project_v2(task_name: str, result: object) -> dict[str, list[object]]:
    if not isinstance(result, dict):
        raise ControlledEvidenceError("a successful V2 sample requires one result object")
    projection = asdict(project_legacy_result(task_name, result))
    return {
        key: list(projection[key])
        for key in sorted(PROJECTION_KEYS)
    }


def _normalize_input(
    kind: str,
    value: dict[str, Any],
    *,
    marketplace_id: str,
    postal_code: str,
) -> tuple[dict[str, Any], str, str]:
    if kind in {"product", "product_hw", "product_time"}:
        plugin = AmazonProductPlugin(None, None, kind=kind)  # type: ignore[arg-type]
    elif kind in {
        "search",
        "search_hour",
        "reviews",
        "category_asin_list",
        "rank_list",
    }:
        plugin = AmazonCollectionPlugin(
            kind=kind,
            fetcher=None,  # type: ignore[arg-type]
            evidence_store=None,  # type: ignore[arg-type]
        )
    else:
        plugin = AmazonMerchantPlugin(
            kind=kind,
            fetcher=None,  # type: ignore[arg-type]
            evidence_store=None,  # type: ignore[arg-type]
        )
    try:
        normalized = plugin.normalize(value, marketplace_id, postal_code)
    except (CrawlerError, TypeError, ValueError) as exc:
        raise ControlledEvidenceError(
            f"shadow scenario {kind} input does not satisfy its V2 contract"
        ) from exc
    normalized_postal = normalized.postal_code or ""
    if not normalized_postal:
        raise ControlledEvidenceError(
            f"shadow scenario {kind} must resolve to a postal code"
        )
    return normalized.as_dict(), normalized.marketplace_id, normalized_postal


def _normalize_comparable(
    value: object,
    *,
    field_name: str | None = None,
    volatile_fields: frozenset[str] = frozenset(),
) -> object:
    if field_name in VOLATILE_TIMESTAMP_FIELDS | volatile_fields:
        if not isinstance(value, str) or not value.strip():
            return value
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
        return "<timestamp>"
    if isinstance(value, dict):
        return {
            str(key): _normalize_comparable(
                child,
                field_name=str(key),
                volatile_fields=volatile_fields,
            )
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [
            _normalize_comparable(child, volatile_fields=volatile_fields)
            for child in value
        ]
    return value


def _differences(left: object, right: object, *, path: str = "$") -> list[str]:
    if type(left) is not type(right):
        return [path]
    if isinstance(left, dict):
        differences: list[str] = []
        for key in sorted(set(left) | set(right)):
            child_path = f"{path}.{key}"
            if key not in left or key not in right:
                differences.append(child_path)
            else:
                differences.extend(_differences(left[key], right[key], path=child_path))
            if len(differences) >= 100:
                return differences[:100]
        return differences
    if isinstance(left, list):
        differences = []
        if len(left) != len(right):
            differences.append(f"{path}.length")
        for index, (left_child, right_child) in enumerate(zip(left, right)):
            differences.extend(
                _differences(left_child, right_child, path=f"{path}[{index}]")
            )
            if len(differences) >= 100:
                return differences[:100]
        return differences
    return [] if left == right else [path]


def _legacy_observation_evidence(
    value: object,
    *,
    response_sha256: str,
) -> dict[str, str] | None:
    if value is None:
        return None
    required = {
        "source",
        "join_strategy",
        "legacy_source_sha256",
        "response_sha256",
    }
    allowed = required | {"response_set_sha256", "execution_adapter"}
    if (
        not isinstance(value, dict)
        or not required.issubset(value)
        or not set(value).issubset(allowed)
    ):
        raise ControlledEvidenceError("legacy observation evidence is invalid")
    if (
        value.get("source") != "current_legacy_source"
        or value.get("join_strategy") != "same_response"
        or not _is_sha256(value.get("legacy_source_sha256"))
        or value.get("response_sha256") != response_sha256
    ):
        raise ControlledEvidenceError(
            "legacy observation does not prove current source on the same response"
        )
    if "response_set_sha256" in value and not _is_sha256(
        value.get("response_set_sha256")
    ):
        raise ControlledEvidenceError("legacy response-set hash is invalid")
    if value.get("execution_adapter") not in {
        None,
        "literal_eval_only",
        "literal_eval_discard_debug_html",
    }:
        raise ControlledEvidenceError("legacy execution adapter is invalid")
    return {str(key): str(child) for key, child in value.items()}


def _validate_check_receipts(
    bundle: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    receipts = bundle.get("check_receipts")
    if not isinstance(receipts, dict) or set(receipts) != REQUIRED_GLOBAL_CHECKS:
        raise ControlledEvidenceError(
            "check_receipts must contain every required controlled check"
        )
    validated: dict[str, dict[str, Any]] = {}
    for check in sorted(REQUIRED_GLOBAL_CHECKS):
        receipt = receipts[check]
        if not isinstance(receipt, dict):
            raise ControlledEvidenceError(f"check receipt {check} must be an object")
        if (
            receipt.get("schema_version") != "controlled-check-receipt.v1"
            or receipt.get("check") != check
            or receipt.get("status") != "passed"
            or receipt.get("environment") != "isolated"
            or receipt.get("run_id") != bundle["run_id"]
            or receipt.get("authorization_reference")
            != bundle["authorization_reference"]
            or not _is_sha256(receipt.get("artifact_sha256"))
        ):
            raise ControlledEvidenceError(f"check receipt {check} is invalid")
        _validate_timestamp(receipt.get("validated_at"))
        validated[check] = dict(receipt)
    return validated


def _build_scenario(
    raw: object,
    *,
    run_id: str,
    authorization_reference: str,
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ControlledEvidenceError("each shadow scenario must be an object")
    kind = raw.get("kind")
    case = raw.get("case")
    if kind not in EXPECTED_TASKS or case not in SCENARIO_CASES:
        raise ControlledEvidenceError("shadow scenario kind or case is unsupported")
    if raw.get("authorized") is not True:
        raise ControlledEvidenceError(f"shadow scenario {kind}/{case} is not authorized")
    marketplace = raw.get("marketplace_id")
    postal_code = raw.get("postal_code")
    input_value = raw.get("input")
    if not isinstance(marketplace, str) or not marketplace.strip():
        raise ControlledEvidenceError(f"shadow scenario {kind}/{case} needs marketplace_id")
    if not isinstance(postal_code, str) or not postal_code.strip():
        raise ControlledEvidenceError(f"shadow scenario {kind}/{case} needs postal_code")
    if not isinstance(input_value, dict) or not input_value:
        raise ControlledEvidenceError(f"shadow scenario {kind}/{case} needs an input object")

    legacy = raw.get("legacy")
    v2 = raw.get("v2")
    if not isinstance(legacy, dict) or not isinstance(v2, dict):
        raise ControlledEvidenceError(f"shadow scenario {kind}/{case} needs legacy and V2 observations")
    legacy_state = legacy.get("state")
    if isinstance(legacy_state, bool) or not isinstance(legacy_state, int):
        raise ControlledEvidenceError(f"shadow scenario {kind}/{case} needs an integer legacy state")
    legacy_projection = _projection_payload(
        legacy.get("projection"),
        path=f"scenario {kind}/{case}.legacy.projection",
    )

    v2_status = v2.get("status")
    v2_error = v2.get("error_code")
    retryable = v2.get("retryable")
    response_sha256 = v2.get("response_sha256")
    if v2_status not in {"succeeded", "failed"}:
        raise ControlledEvidenceError(f"shadow scenario {kind}/{case} has invalid V2 status")
    if v2_error is not None and not isinstance(v2_error, str):
        raise ControlledEvidenceError(f"shadow scenario {kind}/{case} has invalid V2 error")
    if not isinstance(retryable, bool):
        raise ControlledEvidenceError(f"shadow scenario {kind}/{case} needs V2 retryable")
    if not _is_sha256(response_sha256):
        raise ControlledEvidenceError(f"shadow scenario {kind}/{case} needs response SHA-256")
    legacy_observation = _legacy_observation_evidence(
        legacy.get("evidence"),
        response_sha256=str(response_sha256),
    )
    if legacy_observation is not None and "response_set_sha256" in legacy_observation:
        v2_response_set_sha256 = v2.get("response_set_sha256")
        if v2_response_set_sha256 is not None and not _is_sha256(
            v2_response_set_sha256
        ):
            raise ControlledEvidenceError(
                f"shadow scenario {kind}/{case} has an invalid V2 response-set hash"
            )
        if case != "success" and (
            v2_response_set_sha256 != legacy_observation["response_set_sha256"]
        ):
            raise ControlledEvidenceError(
                f"failed shadow scenario {kind}/{case} response-set hash does not match"
            )

    normalized_input, normalized_marketplace, normalized_postal = _normalize_input(
        str(kind),
        input_value,
        marketplace_id=marketplace,
        postal_code=postal_code,
    )
    task_name = KIND_TO_LEGACY_TASK[str(kind)]
    if case == "success":
        v2_projection = _project_v2(task_name, v2.get("result"))
        if not any(v2_projection.values()):
            raise ControlledEvidenceError(
                f"successful shadow scenario {kind}/{case} has an empty projection"
            )
        result_evidence = v2["result"].get("evidence")
        if (
            not isinstance(result_evidence, dict)
            or result_evidence.get("sha256") != response_sha256
        ):
            raise ControlledEvidenceError(
                f"successful shadow scenario {kind}/{case} response hash does not match V2 evidence"
            )
        if legacy_observation is not None and "response_set_sha256" in legacy_observation:
            v2_response_set_sha256 = (
                v2_response_set_sha256
                or result_evidence.get("response_set_sha256")
            )
            if (
                v2_response_set_sha256
                != legacy_observation["response_set_sha256"]
            ):
                raise ControlledEvidenceError(
                    f"successful shadow scenario {kind}/{case} response-set hash does not match V2 evidence"
                )
    else:
        if v2.get("result") is not None:
            raise ControlledEvidenceError(
                f"failed shadow scenario {kind}/{case} must not contain a V2 result"
            )
        v2_projection = {key: [] for key in sorted(PROJECTION_KEYS)}

    # The normal search task writes collection time into data_hour. The hourly
    # task instead receives data_hour as a business dimension and must compare it.
    volatile_fields = frozenset({"data_hour"}) if kind == "search" else frozenset()
    normalized_legacy = _normalize_comparable(
        legacy_projection,
        volatile_fields=volatile_fields,
    )
    normalized_v2 = _normalize_comparable(
        v2_projection,
        volatile_fields=volatile_fields,
    )
    differences = _differences(normalized_legacy, normalized_v2)
    mapped_state = legacy_state_for(str(v2_status), v2_error, task_name)
    outcome_match = mapped_state == legacy_state
    if case == "success":
        outcome_match = outcome_match and v2_status == "succeeded" and not retryable
    elif case == "no_result":
        outcome_match = outcome_match and v2_status == "failed" and not retryable
    else:
        outcome_match = (
            outcome_match
            and v2_status == "failed"
            and retryable
            and legacy_state == -1
        )
    if not outcome_match:
        differences.append("$outcome")

    scenario = {
        "kind": kind,
        "case": case,
        "status": "passed" if not differences else "failed",
        "authorized": True,
        "marketplace_id": normalized_marketplace,
        "postal_code_hash": _digest(normalized_postal),
        "input_hash": _digest(normalized_input),
        "response_sha256": response_sha256,
        "legacy_result_hash": _digest(legacy_projection),
        "v2_result_hash": _digest(v2_projection),
        "field_contract_match": not any(
            difference != "$outcome" for difference in differences
        ),
        "outcome_match": outcome_match,
        "differences": differences,
    }
    if legacy_observation is not None:
        scenario["legacy_observation"] = legacy_observation
    scenario["sample_hash"] = _digest(
        {
            "run_id": run_id,
            "authorization_reference": authorization_reference,
            **scenario,
        }
    )
    return scenario


def build_run_report(bundle: object) -> dict[str, Any]:
    if not isinstance(bundle, dict):
        raise ControlledEvidenceError("controlled shadow bundle must be an object")
    _scan_for_secret_material(bundle, path="bundle")
    if bundle.get("schema_version") != "controlled-shadow-bundle.v1":
        raise ControlledEvidenceError(
            "schema_version must be controlled-shadow-bundle.v1"
        )
    if bundle.get("environment") != "isolated":
        raise ControlledEvidenceError("controlled shadow environment must be isolated")
    for field in ("run_id", "authorization_reference"):
        if not isinstance(bundle.get(field), str) or not bundle[field].strip():
            raise ControlledEvidenceError(f"{field} is required")
    _validate_timestamp(bundle.get("validated_at"))
    runtime = bundle.get("runtime")
    if not isinstance(runtime, dict) or runtime.get("transport_backend") != "curl_cffi":
        raise ControlledEvidenceError("controlled shadow must use curl_cffi")
    if runtime.get("tls_impersonation") is not True:
        raise ControlledEvidenceError("controlled shadow must prove TLS impersonation")
    source_hash = runtime.get("v2_source_sha256")
    current_source_hash = runtime_source_sha256(Path(__file__).resolve().parents[1])
    if not _is_sha256(source_hash) or source_hash != current_source_hash:
        raise ControlledEvidenceError(
            "controlled shadow V2 source fingerprint is missing or stale"
        )
    check_receipts = _validate_check_receipts(bundle)

    raw_scenarios = bundle.get("scenarios")
    if not isinstance(raw_scenarios, list):
        raise ControlledEvidenceError("controlled shadow scenarios must be a list")
    scenarios = [
        _build_scenario(
            raw,
            run_id=bundle["run_id"],
            authorization_reference=bundle["authorization_reference"],
        )
        for raw in raw_scenarios
    ]
    return {
        "schema_version": "controlled-run-report.v1",
        "run_id": bundle["run_id"],
        "authorization_reference": bundle["authorization_reference"],
        "validated_at": bundle["validated_at"],
        "environment": "isolated",
        "runtime": dict(runtime),
        "checks": {check: True for check in sorted(REQUIRED_GLOBAL_CHECKS)},
        "check_receipts": check_receipts,
        "scenarios": scenarios,
    }


def _atomic_write(path: Path, payload: object) -> None:
    if path.is_symlink():
        raise ControlledEvidenceError("refusing to replace a symlinked run report")
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
            "Build a redacted controlled-run report by independently projecting "
            "and comparing legacy/V2 shadow observations. Performs no network "
            "request and no legacy write."
        )
    )
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        bundle = json.loads(args.bundle.read_text(encoding="utf-8"))
        report = build_run_report(bundle)
        _atomic_write(args.output, report)
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        ControlledEvidenceError,
    ) as exc:
        message = (
            str(exc)
            if isinstance(exc, ControlledEvidenceError)
            else "controlled shadow bundle could not be processed"
        )
        print(
            json.dumps(
                {"ok": False, "error": {"type": type(exc).__name__, "message": message}},
                ensure_ascii=False,
            )
        )
        return 2
    failures = [
        f"{scenario['kind']}/{scenario['case']}"
        for scenario in report["scenarios"]
        if scenario["status"] != "passed"
    ]
    print(
        json.dumps(
            {
                "ok": not failures,
                "output": str(args.output),
                "scenario_count": len(report["scenarios"]),
                "failed_scenarios": failures,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
