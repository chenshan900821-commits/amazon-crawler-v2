from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from scripts.build_collection_dual_plan import build_plan
from scripts.build_controlled_run_report import build_run_report
from scripts.collect_collection_dual_shadow import (
    COLLECTION_KINDS,
    validate_collection_dual_plan,
)
from scripts.compile_controlled_evidence import ControlledEvidenceError
from scripts.convert_product_shadow_plan_to_dual import convert_plan
from tests.test_controlled_run_builder import valid_bundle
from tests.test_controlled_v2_shadow import valid_plan


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def collection_plan() -> dict[str, object]:
    product = convert_plan(
        valid_plan(),
        batch_id="product-source",
        kinds=["product"],
    )
    return build_plan(
        product,
        batch_id="collection-source",
        kinds=sorted(COLLECTION_KINDS),
        keyword="wireless mouse",
        category_id="172282",
    )


class CollectionDualShadowTests(unittest.TestCase):
    def test_builder_reuses_only_authorized_inputs_and_receipts(self) -> None:
        plan = collection_plan()

        self.assertEqual(
            {scenario["kind"] for scenario in plan["scenarios"]},
            COLLECTION_KINDS,
        )
        self.assertTrue(
            all("legacy" not in scenario for scenario in plan["scenarios"])
        )
        rank = next(
            scenario for scenario in plan["scenarios"] if scenario["kind"] == "rank_list"
        )
        self.assertEqual(
            rank["input"]["url"],
            "https://www.amazon.com/Best-Sellers-Electronics/zgbs/electronics/172282",
        )

    def test_plan_rejects_precomputed_legacy_observation(self) -> None:
        plan = collection_plan()
        plan["scenarios"][0]["legacy"] = {"state": 1}

        with self.assertRaisesRegex(
            ControlledEvidenceError,
            "must not carry a precomputed legacy result",
        ):
            validate_collection_dual_plan(plan)

    def test_published_schema_matches_runtime_kinds(self) -> None:
        schema = json.loads(
            (
                PROJECT_ROOT
                / "contracts"
                / "controlled-collection-dual-plan.schema.json"
            ).read_text(encoding="utf-8")
        )

        self.assertEqual(
            set(
                schema["properties"]["scenarios"]["items"]["properties"]["kind"][
                    "enum"
                ]
            ),
            COLLECTION_KINDS,
        )

    def test_report_rejects_mismatched_multi_response_proof(self) -> None:
        bundle = copy.deepcopy(valid_bundle())
        scenario = next(
            value
            for value in bundle["scenarios"]
            if value["kind"] == "rank_list" and value["case"] == "success"
        )
        response_hash = scenario["v2"]["response_sha256"]
        scenario["legacy"]["evidence"] = {
            "source": "current_legacy_source",
            "join_strategy": "same_response",
            "legacy_source_sha256": "a" * 64,
            "response_sha256": response_hash,
            "response_set_sha256": "b" * 64,
            "execution_adapter": "literal_eval_only",
        }
        scenario["v2"]["result"]["evidence"]["response_set_sha256"] = "c" * 64

        with self.assertRaisesRegex(
            ControlledEvidenceError,
            "response-set hash does not match",
        ):
            build_run_report(bundle)

    def test_collection_time_is_volatile_but_hour_dimension_is_not(self) -> None:
        bundle = copy.deepcopy(valid_bundle())
        search = next(
            value
            for value in bundle["scenarios"]
            if value["kind"] == "search" and value["case"] == "success"
        )
        search["legacy"]["projection"]["result_rows"][0]["crawl_date"] = (
            "2026-08-23 17:00:00"
        )
        search["legacy"]["projection"]["result_rows"][0]["data_hour"] = (
            "2026-08-23 17:00:00"
        )
        search["v2"]["result"]["data"]["items"][0]["crawl_date"] = (
            "2026-08-23 09:00:01"
        )
        search["v2"]["result"]["data"]["items"][0]["data_hour"] = (
            "2026-08-23 09:00:01"
        )
        report = build_run_report(bundle)
        compared = next(
            value
            for value in report["scenarios"]
            if value["kind"] == "search" and value["case"] == "success"
        )
        self.assertEqual(compared["differences"], [])

        hourly = copy.deepcopy(valid_bundle())
        scenario = next(
            value
            for value in hourly["scenarios"]
            if value["kind"] == "search_hour" and value["case"] == "success"
        )
        scenario["legacy"]["projection"]["result_rows"][0]["data_hour"] = (
            "2026-08-23 17:00:00"
        )
        scenario["v2"]["result"]["data"]["items"][0]["data_hour"] = (
            "2026-08-23 09:00:00"
        )
        report = build_run_report(hourly)
        compared = next(
            value
            for value in report["scenarios"]
            if value["kind"] == "search_hour" and value["case"] == "success"
        )
        self.assertIn("$.result_rows[0].data_hour", compared["differences"])


if __name__ == "__main__":
    unittest.main()
