from __future__ import annotations

import json
import unittest
from pathlib import Path

from scripts.audit_archived_product_parity import (
    _load_current_legacy_merchant_parser,
)
from scripts.build_merchant_dual_plan import build_plan
from scripts.collect_merchant_dual_shadow import (
    MERCHANT_KINDS,
    validate_merchant_dual_plan,
)
from scripts.compile_controlled_evidence import ControlledEvidenceError
from scripts.convert_product_shadow_plan_to_dual import convert_plan
from tests.test_controlled_v2_shadow import valid_plan


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"
REFERENCE_SOURCE_AVAILABLE = (
    (PROJECT_ROOT.parent / "settings/config.py").is_file()
    and (PROJECT_ROOT.parent / "tools/merchant_parser_utils.py").is_file()
)


def merchant_plan() -> dict[str, object]:
    product = convert_plan(
        valid_plan(),
        batch_id="product-source",
        kinds=["product"],
    )
    return build_plan(
        product,
        batch_id="merchant-source",
        kinds=sorted(MERCHANT_KINDS),
        seller_id="ACSFBZX3I4JAS",
        postal_code="98942",
        add_date="20260708",
    )


class MerchantDualShadowTests(unittest.TestCase):
    def test_builder_copies_no_precomputed_legacy_result(self) -> None:
        plan = merchant_plan()

        self.assertEqual(
            {scenario["kind"] for scenario in plan["scenarios"]},
            MERCHANT_KINDS,
        )
        self.assertTrue(
            all("legacy" not in scenario for scenario in plan["scenarios"])
        )
        products = next(
            scenario
            for scenario in plan["scenarios"]
            if scenario["kind"] == "merchant_products"
        )
        self.assertEqual(products["input"]["page"], 1)
        self.assertNotIn("source_task_id", products["input"])

    def test_plan_rejects_precomputed_legacy_observation(self) -> None:
        plan = merchant_plan()
        plan["scenarios"][0]["legacy"] = {"state": 1}

        with self.assertRaisesRegex(
            ControlledEvidenceError,
            "must not carry a precomputed legacy result",
        ):
            validate_merchant_dual_plan(plan)

    def test_published_schema_matches_runtime_kinds(self) -> None:
        schema = json.loads(
            (
                PROJECT_ROOT
                / "contracts"
                / "controlled-merchant-dual-plan.schema.json"
            ).read_text(encoding="utf-8")
        )

        self.assertEqual(
            set(
                schema["properties"]["scenarios"]["items"]["properties"]["kind"][
                    "enum"
                ]
            ),
            MERCHANT_KINDS,
        )

    @unittest.skipUnless(
        REFERENCE_SOURCE_AVAILABLE,
        "optional migration-reference source is not present in this checkout",
    )
    def test_current_legacy_merchant_source_can_be_loaded_safely(self) -> None:
        parser = _load_current_legacy_merchant_parser(PROJECT_ROOT.parent)
        task = {
            "seller_id": "SELLER123",
            "market_id": "ATVPDKIKX0DER",
            "post_code": "10001",
            "add_date": "20260823",
            "page": 1,
        }
        detail = parser(
            "merchant",
            (FIXTURES / "merchant_detail.html").read_text(encoding="utf-8"),
            200,
            task,
            "https://www.amazon.com/sp?seller=SELLER123",
        )
        home = parser(
            "merchant_home",
            (FIXTURES / "merchant_home.html").read_text(encoding="utf-8"),
            200,
            task,
            "https://www.amazon.com/s?me=SELLER123",
        )
        stream = (
            '["dispatch","meta",{"totalResultCount":1,"x":0}]&&&'
            + (FIXTURES / "search_stream.txt").read_text(encoding="utf-8")
        )
        products = parser(
            "merchant_products",
            stream,
            200,
            task,
            "https://www.amazon.com/s/query?me=SELLER123",
        )

        self.assertEqual(detail["seller_name"], "Example Merchant")
        self.assertEqual([row["page"] for row in home], [1, 2, 3])
        self.assertEqual(products[0]["data_asin"], "B000000010")


if __name__ == "__main__":
    unittest.main()
