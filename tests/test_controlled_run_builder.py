from __future__ import annotations

import copy
import hashlib
import json
import unittest
from dataclasses import asdict
from pathlib import Path

from amazon_crawler.infra.legacy_compat import (
    LEGACY_TASKS,
    legacy_state_for,
    project_legacy_result,
)
from scripts.build_controlled_run_report import (
    KIND_TO_LEGACY_TASK,
    build_run_report,
)
from scripts.compile_controlled_evidence import (
    ControlledEvidenceError,
    validate_run_report,
)
from scripts.verify_parity_manifest import EXPECTED_TASKS
from scripts.runtime_source_fingerprint import runtime_source_sha256


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


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


def input_for(kind: str, marketplace: str) -> dict[str, object]:
    common = {"market_id": marketplace, "post_code": "10001" if marketplace == "US" else "100-0001"}
    if kind in {"product", "product_hw", "product_time"}:
        return {**common, "asin": "B000000001"}
    if kind in {"search", "search_hour"}:
        return {**common, "keyword": "authorized fixture", "turn_page": 1, "frequent": 0}
    if kind == "reviews":
        return {**common, "asin": "B000000001"}
    if kind == "category_asin_list":
        return {**common, "category_id": "100", "page": 1}
    if kind == "rank_list":
        domain = "www.amazon.com" if marketplace == "US" else "www.amazon.co.jp"
        return {
            **common,
            "url": f"https://{domain}/bestsellers",
            "url_type": "bestsellers",
            "category_id": "100",
            "page": 1,
        }
    value: dict[str, object] = {**common, "seller_id": "SELLER123"}
    if kind == "merchant_products":
        value["page"] = 1
    return value


def success_result(kind: str, response_sha: str) -> dict[str, object]:
    timestamp = "2026-08-22T09:00:00+08:00"
    if kind in {"product", "product_hw", "product_time"}:
        data: dict[str, object] = {
            "asin": "B000000001",
            "market_id": "US",
            "title": "Authorized sample",
            "created_at": timestamp,
        }
    elif kind == "merchant":
        data = {
            "item": {
                "seller_id": "SELLER123",
                "seller_name": "Authorized seller",
                "created_at": timestamp,
            }
        }
    elif kind == "merchant_home":
        data = {
            "child_tasks": [
                {
                    "seller_id": "SELLER123",
                    "page": 1,
                    "created_at": timestamp,
                }
            ]
        }
    elif kind == "merchant_products":
        data = {
            "items": [
                {
                    "seller_id": "SELLER123",
                    "data_asin": "B000000001",
                    "created_at": timestamp,
                }
            ],
            "product_detail_tasks": [],
        }
    else:
        data = {
            "items": [
                {
                    "asin": "B000000001",
                    "created_at": timestamp,
                }
            ]
        }
    return {"data": data, "evidence": {"sha256": response_sha}}


def projection_for(task_name: str, result: dict[str, object]) -> dict[str, object]:
    return json.loads(json.dumps(asdict(project_legacy_result(task_name, result))))


def valid_bundle() -> dict[str, object]:
    run_id = "isolated-run-20260822"
    authorization = "change-ticket-123"
    validated_at = "2026-08-22T09:00:00+08:00"
    scenarios = []
    for index, kind in enumerate(sorted(EXPECTED_TASKS)):
        marketplace = "US" if index % 2 == 0 else "JP"
        postal = "10001" if marketplace == "US" else "100-0001"
        task_name = KIND_TO_LEGACY_TASK[kind]
        for case in ("success", "no_result", "throttle"):
            response_sha = digest(f"response:{kind}:{case}")
            if case == "success":
                result = success_result(kind, response_sha)
                legacy_projection = projection_for(task_name, result)
                legacy_state = 1
                v2_status = "succeeded"
                error_code = None
                retryable = False
            elif case == "no_result":
                result = None
                error_code = NO_RESULT_CODES[kind]
                legacy_state = legacy_state_for("failed", error_code, task_name)
                v2_status = "failed"
                retryable = False
                legacy_projection = {
                    "result_rows": [],
                    "dimension_rows": [],
                    "child_task_rows": [],
                    "product_task_rows": [],
                }
            else:
                result = None
                error_code = "upstream_retryable"
                legacy_state = -1
                v2_status = "failed"
                retryable = True
                legacy_projection = {
                    "result_rows": [],
                    "dimension_rows": [],
                    "child_task_rows": [],
                    "product_task_rows": [],
                }
            scenarios.append(
                {
                    "kind": kind,
                    "case": case,
                    "authorized": True,
                    "marketplace_id": marketplace,
                    "postal_code": postal,
                    "input": input_for(kind, marketplace),
                    "legacy": {
                        "state": legacy_state,
                        "projection": legacy_projection,
                    },
                    "v2": {
                        "status": v2_status,
                        "error_code": error_code,
                        "retryable": retryable,
                        "response_sha256": response_sha,
                        "result": result,
                    },
                }
            )
    checks = (
        "cookie_proxy_redaction",
        "checkpoint_recovery",
        "legacy_bridge_roundtrip",
        "tls_impersonation",
    )
    return {
        "schema_version": "controlled-shadow-bundle.v1",
        "run_id": run_id,
        "authorization_reference": authorization,
        "validated_at": validated_at,
        "environment": "isolated",
        "runtime": {
            "transport_backend": "curl_cffi",
            "tls_impersonation": True,
            "v2_source_sha256": runtime_source_sha256(PROJECT_ROOT),
        },
        "check_receipts": {
            check: {
                "schema_version": "controlled-check-receipt.v1",
                "check": check,
                "status": "passed",
                "environment": "isolated",
                "run_id": run_id,
                "authorization_reference": authorization,
                "validated_at": validated_at,
                "artifact_sha256": digest(f"artifact:{check}"),
            }
            for check in checks
        },
        "scenarios": scenarios,
    }


class ControlledRunBuilderTests(unittest.TestCase):
    def test_builder_computes_a_compiler_valid_report_without_raw_inputs(self) -> None:
        report = build_run_report(valid_bundle())

        self.assertIs(validate_run_report(report), report)
        self.assertEqual(len(report["scenarios"]), 33)
        rendered = json.dumps(report, ensure_ascii=False, sort_keys=True)
        self.assertNotIn("100-0001", rendered)
        self.assertNotIn("authorized fixture", rendered)

    def test_builder_computes_field_differences_instead_of_trusting_booleans(self) -> None:
        bundle = valid_bundle()
        success = next(
            scenario
            for scenario in bundle["scenarios"]
            if scenario["kind"] == "merchant" and scenario["case"] == "success"
        )
        success["legacy"]["projection"]["result_rows"][0]["seller_name"] = "Different"

        report = build_run_report(bundle)
        scenario = next(
            value
            for value in report["scenarios"]
            if value["kind"] == "merchant" and value["case"] == "success"
        )

        self.assertEqual(scenario["status"], "failed")
        self.assertFalse(scenario["field_contract_match"])
        self.assertIn("$.result_rows[0].seller_name", scenario["differences"])
        with self.assertRaisesRegex(ControlledEvidenceError, "did not pass"):
            validate_run_report(report)

    def test_success_response_hash_must_come_from_v2_evidence(self) -> None:
        bundle = valid_bundle()
        success = next(
            scenario
            for scenario in bundle["scenarios"]
            if scenario["case"] == "success"
        )
        success["v2"]["response_sha256"] = digest("different-response")

        with self.assertRaisesRegex(
            ControlledEvidenceError,
            "response hash does not match V2 evidence",
        ):
            build_run_report(bundle)

    def test_invalid_task_input_and_secret_fields_are_rejected(self) -> None:
        invalid = valid_bundle()
        invalid["scenarios"][0]["input"] = {"keyword": "missing marketplace"}
        with self.assertRaisesRegex(
            ControlledEvidenceError,
            "input does not satisfy its V2 contract",
        ):
            build_run_report(invalid)

        secret = copy.deepcopy(valid_bundle())
        secret["cookie"] = "must-not-appear"
        with self.assertRaisesRegex(ControlledEvidenceError, "forbidden secret field"):
            build_run_report(secret)

    def test_stale_v2_source_fingerprint_is_rejected(self) -> None:
        bundle = valid_bundle()
        bundle["runtime"]["v2_source_sha256"] = "f" * 64

        with self.assertRaisesRegex(ControlledEvidenceError, "source fingerprint"):
            build_run_report(bundle)


if __name__ == "__main__":
    unittest.main()
