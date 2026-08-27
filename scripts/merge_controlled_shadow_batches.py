#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scripts.build_controlled_run_report import _build_scenario, _validate_check_receipts
from scripts.collect_controlled_v2_shadow import (
    BATCH_BUNDLE_SCHEMA,
    FINAL_BUNDLE_SCHEMA,
    _atomic_write_outside_project,
    _missing_scenarios,
    _validate_source_evidence,
    _validate_scenarios,
)
from scripts.compile_controlled_evidence import (
    REQUIRED_GLOBAL_CHECKS,
    ControlledEvidenceError,
    _is_sha256,
    _scan_for_secret_material,
    _validate_timestamp,
)
from scripts.runtime_source_fingerprint import runtime_source_sha256


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _validate_batch(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ControlledEvidenceError("controlled shadow batch must be an object")
    _scan_for_secret_material(value, path="batch")
    if value.get("schema_version") != BATCH_BUNDLE_SCHEMA:
        raise ControlledEvidenceError(
            f"batch schema_version must be {BATCH_BUNDLE_SCHEMA}"
        )
    if value.get("environment") != "isolated":
        raise ControlledEvidenceError("controlled shadow batch must be isolated")
    for field in ("batch_id", "run_id", "authorization_reference"):
        if not isinstance(value.get(field), str) or not value[field].strip():
            raise ControlledEvidenceError(f"batch {field} is required")
    _validate_timestamp(value.get("validated_at"))
    runtime = value.get("runtime")
    if not isinstance(runtime, dict) or runtime.get("transport_backend") != "curl_cffi":
        raise ControlledEvidenceError("controlled shadow batch must use curl_cffi")
    if runtime.get("tls_impersonation") is not True:
        raise ControlledEvidenceError(
            "controlled shadow batch must prove TLS impersonation"
        )
    source_hash = runtime.get("v2_source_sha256")
    if source_hash is not None and not _is_sha256(source_hash):
        raise ControlledEvidenceError(
            "controlled shadow batch V2 source fingerprint is invalid"
        )
    _validate_check_receipts(value)
    source_evidence = value.get("source_evidence")
    if source_evidence is not None:
        _validate_source_evidence(
            source_evidence,
            path="controlled shadow batch source_evidence",
        )
    scenarios = value.get("scenarios")
    if not isinstance(scenarios, list):
        raise ControlledEvidenceError("controlled shadow batch scenarios must be a list")
    identities = _validate_scenarios(
        {"scenarios": scenarios},
        require_complete=False,
    )
    for scenario in scenarios:
        _build_scenario(
            scenario,
            run_id=value["run_id"],
            authorization_reference=value["authorization_reference"],
        )
    missing = _missing_scenarios(identities)
    if value.get("missing_scenarios") != missing:
        raise ControlledEvidenceError("controlled shadow batch missing_scenarios is stale")
    if value.get("matrix_complete") is not (not missing):
        raise ControlledEvidenceError("controlled shadow batch matrix_complete is stale")
    return value


def batch_status(batches: list[object]) -> dict[str, Any]:
    if not batches:
        raise ControlledEvidenceError("at least one controlled shadow batch is required")
    validated = [_validate_batch(value) for value in batches]
    first = validated[0]
    identities: set[tuple[str, str]] = set()
    batch_ids: set[str] = set()
    comparison_summaries: list[dict[str, Any]] = []
    diagnostic_only_batches: list[str] = []
    source_hashes: list[str] = []
    marketplaces: set[str] = set()
    postal_hashes: set[str] = set()
    for batch in validated:
        if batch["batch_id"] in batch_ids:
            raise ControlledEvidenceError("duplicate controlled shadow batch_id")
        batch_ids.add(batch["batch_id"])
        source_evidence = batch.get("source_evidence")
        if (
            isinstance(source_evidence, dict)
            and source_evidence.get("join_strategy")
            == "asin_market_postal_latest"
        ):
            diagnostic_only_batches.append(batch["batch_id"])
        for field in ("run_id", "authorization_reference", "environment"):
            if batch[field] != first[field]:
                raise ControlledEvidenceError(
                    f"controlled shadow batches disagree on {field}"
                )
        runtime = batch["runtime"]
        if (
            runtime.get("transport_backend")
            != first["runtime"].get("transport_backend")
            or runtime.get("tls_impersonation")
            is not first["runtime"].get("tls_impersonation")
        ):
            raise ControlledEvidenceError(
                "controlled shadow batches disagree on transport runtime"
            )
        source_hash = runtime.get("v2_source_sha256")
        if isinstance(source_hash, str):
            source_hashes.append(source_hash)
        for check in sorted(REQUIRED_GLOBAL_CHECKS - {"tls_impersonation"}):
            if batch["check_receipts"][check] != first["check_receipts"][check]:
                raise ControlledEvidenceError(
                    f"controlled shadow batches disagree on {check} receipt"
                )
        for scenario in batch["scenarios"]:
            identity = (str(scenario["kind"]), str(scenario["case"]))
            if identity in identities:
                raise ControlledEvidenceError(
                    f"duplicate controlled shadow scenario {identity[0]}/{identity[1]}"
                )
            identities.add(identity)
            comparison = _build_scenario(
                scenario,
                run_id=batch["run_id"],
                authorization_reference=batch["authorization_reference"],
            )
            marketplaces.add(str(comparison["marketplace_id"]).upper())
            postal_hashes.add(str(comparison["postal_code_hash"]))
            comparison_summaries.append(
                {
                    "kind": comparison["kind"],
                    "case": comparison["case"],
                    "status": comparison["status"],
                    "field_contract_match": comparison["field_contract_match"],
                    "outcome_match": comparison["outcome_match"],
                    "difference_count": len(comparison["differences"]),
                    "differences": list(comparison["differences"]),
                    "sample_hash": comparison["sample_hash"],
                }
            )
    missing = _missing_scenarios(identities)
    unique_source_hashes = sorted(set(source_hashes))
    if len(unique_source_hashes) > 1:
        raise ControlledEvidenceError(
            "controlled shadow batches were collected from different V2 sources"
        )
    source_binding_complete = len(source_hashes) == len(validated)
    current_source_hash = runtime_source_sha256(Path(__file__).resolve().parents[1])
    source_matches_current = (
        source_binding_complete
        and len(unique_source_hashes) == 1
        and unique_source_hashes[0] == current_source_hash
    )
    coverage_missing = []
    for marketplace in ("US", "JP"):
        if marketplace not in marketplaces:
            coverage_missing.append(f"marketplace:{marketplace}")
    if len(postal_hashes) < 2:
        coverage_missing.append("postal_codes:at_least_two")
    coverage_ready = not coverage_missing
    return {
        "schema_version": "controlled-shadow-batch-status.v1",
        "run_id": first["run_id"],
        "batch_ids": sorted(batch_ids),
        "scenario_count": len(identities),
        "matrix_complete": not missing,
        "merge_eligible": (
            not missing
            and not diagnostic_only_batches
            and source_binding_complete
            and source_matches_current
            and coverage_ready
        ),
        "coverage_ready": coverage_ready,
        "coverage": {
            "marketplaces": sorted(marketplaces),
            "postal_code_count": len(postal_hashes),
            "missing": coverage_missing,
        },
        "source_binding_complete": source_binding_complete,
        "source_matches_current": source_matches_current,
        "current_v2_source_sha256": current_source_hash,
        "v2_source_sha256": (
            unique_source_hashes[0] if source_binding_complete else None
        ),
        "diagnostic_only_batch_ids": sorted(diagnostic_only_batches),
        "missing_scenarios": missing,
        "passed_scenarios": sum(
            value["status"] == "passed" for value in comparison_summaries
        ),
        "failed_scenarios": sum(
            value["status"] == "failed" for value in comparison_summaries
        ),
        "scenario_statuses": sorted(
            comparison_summaries,
            key=lambda value: (str(value["kind"]), str(value["case"])),
        ),
    }


def merge_batches(batches: list[object]) -> dict[str, Any]:
    status = batch_status(batches)
    if not status["matrix_complete"]:
        raise ControlledEvidenceError(
            "controlled shadow batches are incomplete: "
            + ", ".join(status["missing_scenarios"])
        )
    if status["diagnostic_only_batch_ids"]:
        raise ControlledEvidenceError(
            "controlled shadow batches include historical ASIN fallback evidence, "
            "which is diagnostic only and cannot enter the final bundle"
        )
    if not status["source_binding_complete"]:
        raise ControlledEvidenceError(
            "controlled shadow batches are not bound to one V2 source fingerprint"
        )
    if not status["source_matches_current"]:
        raise ControlledEvidenceError(
            "controlled shadow batches are stale for the current V2 source fingerprint"
        )
    if not status["coverage_ready"]:
        raise ControlledEvidenceError(
            "controlled shadow batches lack required geographic coverage: "
            + ", ".join(status["coverage"]["missing"])
        )
    validated = [_validate_batch(value) for value in batches]
    first = validated[0]
    scenarios = [
        scenario
        for batch in validated
        for scenario in batch["scenarios"]
    ]
    _validate_scenarios({"scenarios": scenarios}, require_complete=True)
    validated_at = datetime.now(UTC).isoformat()
    check_receipts = {
        check: first["check_receipts"][check]
        for check in sorted(REQUIRED_GLOBAL_CHECKS - {"tls_impersonation"})
    }
    check_receipts["tls_impersonation"] = {
        "schema_version": "controlled-check-receipt.v1",
        "check": "tls_impersonation",
        "status": "passed",
        "environment": "isolated",
        "run_id": first["run_id"],
        "authorization_reference": first["authorization_reference"],
        "validated_at": validated_at,
        "artifact_sha256": _digest(
            {
                "transport": first["runtime"],
                "batch_ids": status["batch_ids"],
                "batch_tls_artifacts": sorted(
                    batch["check_receipts"]["tls_impersonation"]["artifact_sha256"]
                    for batch in validated
                ),
                "response_hashes": sorted(
                    str(scenario["v2"]["response_sha256"])
                    for scenario in scenarios
                ),
            }
        ),
    }
    bundle = {
        "schema_version": FINAL_BUNDLE_SCHEMA,
        "run_id": first["run_id"],
        "authorization_reference": first["authorization_reference"],
        "validated_at": validated_at,
        "environment": "isolated",
        "runtime": dict(first["runtime"]),
        "check_receipts": check_receipts,
        "scenarios": sorted(
            scenarios,
            key=lambda value: (str(value["kind"]), str(value["case"])),
        ),
    }
    _scan_for_secret_material(bundle, path="bundle")
    return bundle


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Inspect or merge incremental controlled shadow batches. "
            "A final bundle is emitted only when all 33 unique scenarios are present."
        )
    )
    parser.add_argument("batches", nargs="+", type=Path)
    parser.add_argument("--status-only", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    project_root = Path(__file__).resolve().parents[1]
    try:
        batches = [json.loads(path.read_text(encoding="utf-8")) for path in args.batches]
        if args.status_only:
            result = batch_status(batches)
        else:
            if args.output is None:
                raise ControlledEvidenceError("batch merge requires --output")
            result = merge_batches(batches)
            _atomic_write_outside_project(
                args.output,
                result,
                project_root=project_root,
            )
            result = {
                "ok": True,
                "output": str(args.output.resolve()),
                "scenario_count": len(result["scenarios"]),
                "matrix_complete": True,
            }
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        ControlledEvidenceError,
        ValueError,
    ) as exc:
        message = (
            str(exc)
            if isinstance(exc, ControlledEvidenceError)
            else "controlled shadow batch merge failed"
        )
        print(
            json.dumps(
                {"ok": False, "error": {"type": type(exc).__name__, "message": message}},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
