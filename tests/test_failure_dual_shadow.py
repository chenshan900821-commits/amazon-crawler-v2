from __future__ import annotations

import copy
import json
import unittest
from types import SimpleNamespace

from amazon_crawler.domain.models import CrawlFailure, CrawlResult, PluginOutcome
from scripts.build_controlled_run_report import build_run_report
from scripts.build_failure_dual_plan import build_plan
from scripts.collect_failure_dual_shadow import (
    DIAGNOSTIC_SCHEMA,
    FailureCandidateMismatch,
    _candidate_diagnostic,
    _candidate_diagnostic_report,
    _legacy_failure_state,
    _safe_legacy_failure_reason,
    _validated_failure_case,
    validate_failure_dual_plan,
)
from scripts.compile_controlled_evidence import ControlledEvidenceError
from scripts.convert_product_shadow_plan_to_dual import convert_plan
from scripts.merge_controlled_shadow_batches import batch_status
from tests.test_controlled_run_builder import valid_bundle
from tests.test_controlled_v2_shadow import valid_plan


def product_source() -> dict[str, object]:
    return convert_plan(
        valid_plan(),
        batch_id="product-source",
        kinds=["product"],
    )


class NoPageError(Exception):
    pass


class NoReviewError(Exception):
    pass


class FailureDualShadowTests(unittest.TestCase):
    def test_observed_throttle_requires_explicit_acceptance(self) -> None:
        blocked = CrawlFailure("blocked_page", "blocked", retryable=True)

        self.assertIsNone(
            _validated_failure_case(
                kind="merchant_home",
                planned_case="no_result",
                failure=blocked,
                accept_observed_throttle=False,
            )
        )
        self.assertEqual(
            _validated_failure_case(
                kind="merchant_home",
                planned_case="no_result",
                failure=blocked,
                accept_observed_throttle=True,
            ),
            "throttle",
        )
        self.assertIsNone(
            _validated_failure_case(
                kind="merchant_home",
                planned_case="throttle",
                failure=CrawlFailure(
                    "merchant_has_no_products", "missing", retryable=False
                ),
                accept_observed_throttle=True,
            )
        )

    def test_rejected_candidate_diagnostic_keeps_only_shape_counts_and_hashes(self) -> None:
        sentinel = "cookie=session-secret"
        response = SimpleNamespace(
            html=f"<html>{sentinel}</html>",
            status_code=200,
            content_sha256="a" * 64,
            byte_count=35,
        )
        outcome = PluginOutcome(
            result=CrawlResult(
                data={
                    "items": [{"title": sentinel}],
                    "row_count": 1,
                    "task": {"private": sentinel},
                },
                schema_version="amazon.search.v1",
            )
        )

        diagnostic = _candidate_diagnostic(
            kind="category_asin_list",
            case="no_result",
            normalized_input={"category_id": sentinel},
            outcome=outcome,
            responses=[response],
        )
        rendered = json.dumps(diagnostic, sort_keys=True)

        self.assertNotIn(sentinel, rendered)
        self.assertEqual(diagnostic["observed"]["status"], "succeeded")
        self.assertEqual(diagnostic["observed"]["row_count"], 1)
        self.assertEqual(diagnostic["observed"]["item_count"], 1)
        self.assertEqual(diagnostic["response_count"], 1)
        self.assertEqual(diagnostic["responses"][0]["content_sha256"], "a" * 64)

    def test_candidate_diagnostic_keeps_only_a_safe_error_class_name(self) -> None:
        safe = _candidate_diagnostic(
            kind="product",
            case="throttle",
            normalized_input={"asin": "B000000000"},
            outcome=PluginOutcome(
                failure=CrawlFailure(
                    "network_error",
                    "failed",
                    retryable=True,
                    details={"error_type": "RequestsError"},
                )
            ),
            responses=[],
        )
        unsafe = _candidate_diagnostic(
            kind="product",
            case="throttle",
            normalized_input={"asin": "B000000000"},
            outcome=PluginOutcome(
                failure=CrawlFailure(
                    "network_error",
                    "failed",
                    retryable=True,
                    details={"error_type": "RequestsError cookie=session-secret"},
                )
            ),
            responses=[],
        )

        self.assertEqual(safe["observed"]["error_type"], "RequestsError")
        self.assertIsNone(unsafe["observed"]["error_type"])

    def test_failure_candidate_report_is_explicitly_non_promotable(self) -> None:
        sentinel = "proxy-password-secret"
        diagnostic = _candidate_diagnostic(
            kind="search_hour",
            case="no_result",
            normalized_input={"keyword": sentinel},
            outcome=PluginOutcome(
                failure=CrawlFailure(
                    "network_error",
                    "transport failed",
                    retryable=True,
                    details={
                        "error_type": "TimeoutError",
                        "proxy_password": sentinel,
                    },
                )
            ),
            responses=[],
        )
        mismatch = FailureCandidateMismatch("candidate missed", diagnostic)
        plan = build_plan(
            product_source(),
            batch_id="candidate-diagnostic",
            kinds=["search_hour"],
            case="no_result",
            seller_id="ACSFBZX3I4JAS",
            postal_code=None,
            asin=None,
            keyword="missing-item",
        )

        report = _candidate_diagnostic_report(plan, mismatch)
        rendered = json.dumps(report, sort_keys=True)

        self.assertEqual(report["schema_version"], DIAGNOSTIC_SCHEMA)
        self.assertIs(report["promotable"], False)
        self.assertEqual(report["diagnostic"]["response_count"], 0)
        self.assertNotIn("proxy_password", rendered)
        self.assertNotIn(sentinel, rendered)
        with self.assertRaisesRegex(
            ControlledEvidenceError,
            "batch schema_version must be controlled-shadow-batch.v1",
        ):
            batch_status([report])

    def test_builder_creates_missing_product_inputs_without_legacy_results(self) -> None:
        plan = build_plan(
            product_source(),
            batch_id="missing-products",
            kinds=["product", "product_hw", "product_time"],
            case="no_result",
            seller_id="ACSFBZX3I4JAS",
            postal_code=None,
            asin=None,
            keyword=None,
        )

        self.assertTrue(
            all(scenario["input"]["asin"] == "B000000000" for scenario in plan["scenarios"])
        )
        self.assertTrue(
            all("legacy" not in scenario for scenario in plan["scenarios"])
        )

    def test_plan_rejects_precomputed_legacy_observation(self) -> None:
        plan = build_plan(
            product_source(),
            batch_id="missing-product",
            kinds=["product"],
            case="no_result",
            seller_id="ACSFBZX3I4JAS",
            postal_code=None,
            asin=None,
            keyword=None,
        )
        plan["scenarios"][0]["legacy"] = {"state": -3}

        with self.assertRaisesRegex(
            ControlledEvidenceError,
            "must not carry a precomputed legacy result",
        ):
            validate_failure_dual_plan(plan)

    def test_builder_accepts_observed_failure_sample_overrides(self) -> None:
        plan = build_plan(
            product_source(),
            batch_id="observed-failures",
            kinds=[
                "search",
                "reviews",
                "category_asin_list",
                "rank_list",
                "merchant_products",
            ],
            case="no_result",
            seller_id="ACSFBZX3I4JAS",
            postal_code="10001",
            asin="B0DTKD654R",
            keyword="controlled-empty-sample",
            search_page=400,
            category_id="123456789",
            category_page=400,
            rank_url="https://www.amazon.com/gp/new-releases/123456789",
            rank_url_type="new-releases",
            merchant_products_seller_id="ACSFBZX3I4JAS",
            merchant_products_page=99,
        )
        inputs = {scenario["kind"]: scenario["input"] for scenario in plan["scenarios"]}

        self.assertEqual(inputs["search"]["keyword"], "controlled-empty-sample")
        self.assertEqual(inputs["search"]["turn_page"], 400)
        self.assertEqual(inputs["reviews"]["asin"], "B0DTKD654R")
        self.assertEqual(
            inputs["category_asin_list"]["category_id"], "123456789"
        )
        self.assertEqual(inputs["category_asin_list"]["page"], 400)
        self.assertEqual(
            inputs["rank_list"]["url"],
            "https://www.amazon.com/gp/new-releases/123456789",
        )
        self.assertEqual(inputs["rank_list"]["url_type"], "new-releases")
        self.assertEqual(
            inputs["merchant_products"]["seller_id"], "ACSFBZX3I4JAS"
        )
        self.assertEqual(inputs["merchant_products"]["page"], 99)

    def test_builder_can_authorize_an_explicit_marketplace_coverage_sample(self) -> None:
        plan = build_plan(
            product_source(),
            batch_id="jp-coverage",
            kinds=["product"],
            case="no_result",
            seller_id="ACSFBZX3I4JAS",
            postal_code="100-0001",
            marketplace_code="JP",
        )

        scenario = plan["scenarios"][0]
        self.assertEqual(scenario["marketplace_id"], "JP")
        self.assertEqual(scenario["postal_code"], "100-0001")
        self.assertEqual(scenario["input"]["market_id"], "A1VC38T7YXB528")
        self.assertEqual(scenario["input"]["post_code"], "100-0001")

    def test_legacy_exception_types_map_to_old_terminal_states(self) -> None:
        self.assertEqual(_legacy_failure_state(NoPageError()), -3)
        self.assertEqual(_legacy_failure_state(NoReviewError()), -4)
        self.assertEqual(_legacy_failure_state(Exception()), -1)
        self.assertEqual(
            _safe_legacy_failure_reason(Exception("商品数量异常")),
            "missing_product_total",
        )

    def test_failed_scenario_response_set_must_match_same_response_proof(self) -> None:
        bundle = copy.deepcopy(valid_bundle())
        scenario = next(
            value
            for value in bundle["scenarios"]
            if value["kind"] == "product" and value["case"] == "no_result"
        )
        response_hash = scenario["v2"]["response_sha256"]
        scenario["legacy"]["evidence"] = {
            "source": "current_legacy_source",
            "join_strategy": "same_response",
            "legacy_source_sha256": "a" * 64,
            "response_sha256": response_hash,
            "response_set_sha256": "b" * 64,
        }
        scenario["v2"]["response_set_sha256"] = "c" * 64

        with self.assertRaisesRegex(
            ControlledEvidenceError,
            "response-set hash does not match",
        ):
            build_run_report(bundle)


if __name__ == "__main__":
    unittest.main()
