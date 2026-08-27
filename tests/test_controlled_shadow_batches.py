from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from scripts.build_controlled_run_report import build_run_report
from scripts.collect_controlled_v2_shadow import (
    BATCH_BUNDLE_SCHEMA,
    _missing_scenarios,
    collect_shadow_batch,
    validate_shadow_batch_plan,
    validate_shadow_plan,
)
from scripts.compile_controlled_evidence import (
    ControlledEvidenceError,
    validate_run_report,
)
from scripts.merge_controlled_shadow_batches import batch_status, merge_batches
from scripts.export_legacy_product_shadow_batch_plan import export_plan
from tests.test_controlled_run_builder import input_for, valid_bundle
from tests.test_controlled_v2_shadow import fake_application, valid_plan


def batch_plan(size: int = 2) -> dict[str, object]:
    plan = copy.deepcopy(valid_plan())
    plan["schema_version"] = "controlled-shadow-batch-plan.v1"
    plan["batch_id"] = "batch-001"
    plan["scenarios"] = plan["scenarios"][:size]
    return plan


def batch_from_bundle(
    start: int,
    end: int,
    *,
    batch_id: str,
) -> dict[str, object]:
    source = valid_bundle()
    scenarios = copy.deepcopy(source["scenarios"][start:end])
    identities = {
        (str(value["kind"]), str(value["case"])) for value in scenarios
    }
    missing = _missing_scenarios(identities)
    return {
        "schema_version": BATCH_BUNDLE_SCHEMA,
        "batch_id": batch_id,
        "run_id": source["run_id"],
        "authorization_reference": source["authorization_reference"],
        "validated_at": source["validated_at"],
        "environment": source["environment"],
        "runtime": copy.deepcopy(source["runtime"]),
        "check_receipts": copy.deepcopy(source["check_receipts"]),
        "scenarios": scenarios,
        "matrix_complete": not missing,
        "missing_scenarios": missing,
    }


class ControlledShadowBatchTests(unittest.IsolatedAsyncioTestCase):
    def test_legacy_product_success_can_be_exported_as_a_private_batch_plan(self) -> None:
        import sqlalchemy

        with tempfile.TemporaryDirectory() as tempdir:
            database = Path(tempdir) / "legacy.db"
            engine = sqlalchemy.create_engine(f"sqlite:///{database}")
            metadata = sqlalchemy.MetaData()
            task = sqlalchemy.Table(
                "amazon_product_details_jp_task_boost",
                metadata,
                sqlalchemy.Column("id", sqlalchemy.Integer, primary_key=True),
                sqlalchemy.Column("market_id", sqlalchemy.String),
                sqlalchemy.Column("asin", sqlalchemy.String),
                sqlalchemy.Column("post_code", sqlalchemy.String),
                sqlalchemy.Column("add_date", sqlalchemy.String),
                sqlalchemy.Column("state", sqlalchemy.Integer),
            )
            result = sqlalchemy.Table(
                "amazon_product_details",
                metadata,
                sqlalchemy.Column("task_id", sqlalchemy.String, primary_key=True),
                sqlalchemy.Column("asin", sqlalchemy.String),
                sqlalchemy.Column("title", sqlalchemy.String),
            )
            dimensions = sqlalchemy.Table(
                "amazon_dimensions_detail",
                metadata,
                sqlalchemy.Column("id", sqlalchemy.Integer, primary_key=True),
                sqlalchemy.Column("task_id", sqlalchemy.String),
                sqlalchemy.Column("market_place_id", sqlalchemy.String),
                sqlalchemy.Column("parent_asin", sqlalchemy.String),
                sqlalchemy.Column("asin", sqlalchemy.String),
                sqlalchemy.Column("dimensions", sqlalchemy.String),
                sqlalchemy.Column("created_at", sqlalchemy.String),
                sqlalchemy.Column("image_url", sqlalchemy.String),
            )
            metadata.create_all(engine)
            with engine.begin() as connection:
                connection.execute(
                    task.insert(),
                    {
                        "id": 7,
                        "market_id": "ATVPDKIKX0DER",
                        "asin": "B000000001",
                        "post_code": "10001",
                        "add_date": "2026-08-23",
                        "state": 1,
                    },
                )
                connection.execute(
                    result.insert(),
                    {"task_id": "7", "asin": "B000000001", "title": "Sample"},
                )
                connection.execute(
                    dimensions.insert(),
                    {
                        "id": 1,
                        "task_id": "7",
                        "market_place_id": "ATVPDKIKX0DER",
                        "parent_asin": "B000000001",
                        "asin": "B000000002",
                        "dimensions": "Color: Blue",
                    },
                )
            source = valid_plan()
            plan, task_hash = export_plan(
                mysql_url=f"sqlite:///{database}",
                task_name="product_jp",
                run_id=source["run_id"],
                batch_id="legacy-product-success",
                authorization_reference=source["authorization_reference"],
                check_receipts=source["check_receipts"],
            )
            engine.dispose()

        self.assertEqual(plan["scenarios"][0]["kind"], "product")
        self.assertEqual(plan["scenarios"][0]["case"], "success")
        self.assertEqual(
            plan["scenarios"][0]["legacy"]["projection"]["result_rows"][0]["title"],
            "Sample",
        )
        self.assertEqual(len(task_hash), 64)

    def test_partial_plan_is_explicit_and_final_validator_stays_strict(self) -> None:
        plan = batch_plan()

        self.assertIs(validate_shadow_batch_plan(plan), plan)
        with self.assertRaisesRegex(
            ControlledEvidenceError,
            "schema_version must be controlled-shadow-plan.v1",
        ):
            validate_shadow_plan(plan)

    async def test_collector_emits_a_non_promotable_incremental_batch(self) -> None:
        plan = batch_plan()
        batch = await collect_shadow_batch(
            plan,
            application=fake_application(),
            expected_authorization_reference=plan["authorization_reference"],
        )

        self.assertEqual(batch["schema_version"], BATCH_BUNDLE_SCHEMA)
        self.assertEqual(batch["batch_id"], "batch-001")
        self.assertEqual(len(batch["scenarios"]), 2)
        self.assertFalse(batch["matrix_complete"])
        with self.assertRaisesRegex(
            ControlledEvidenceError,
            "schema_version must be controlled-shadow-bundle.v1",
        ):
            build_run_report(batch)

    def test_batches_merge_only_after_the_full_unique_matrix_exists(self) -> None:
        first = batch_from_bundle(0, 16, batch_id="batch-a")
        second = batch_from_bundle(16, 33, batch_id="batch-b")

        status = batch_status([first, second])
        self.assertTrue(status["matrix_complete"])
        self.assertEqual(status["scenario_count"], 33)
        self.assertEqual(status["passed_scenarios"], 33)
        self.assertEqual(status["failed_scenarios"], 0)
        self.assertTrue(status["source_binding_complete"])
        self.assertTrue(status["source_matches_current"])
        self.assertTrue(status["coverage_ready"])
        self.assertEqual(status["coverage"]["marketplaces"], ["JP", "US"])
        self.assertEqual(status["coverage"]["postal_code_count"], 2)
        self.assertTrue(status["merge_eligible"])

        merged = merge_batches([first, second])
        report = build_run_report(merged)
        self.assertIs(validate_run_report(report), report)

    def test_complete_matrix_without_us_jp_and_two_postals_cannot_merge(self) -> None:
        batch = batch_from_bundle(0, 33, batch_id="us-only")
        for scenario in batch["scenarios"]:
            scenario["marketplace_id"] = "US"
            scenario["postal_code"] = "10001"
            scenario["input"] = input_for(str(scenario["kind"]), "US")

        status = batch_status([batch])

        self.assertTrue(status["matrix_complete"])
        self.assertFalse(status["coverage_ready"])
        self.assertEqual(status["coverage"]["marketplaces"], ["US"])
        self.assertEqual(status["coverage"]["postal_code_count"], 1)
        self.assertEqual(
            status["coverage"]["missing"],
            ["marketplace:JP", "postal_codes:at_least_two"],
        )
        self.assertFalse(status["merge_eligible"])
        with self.assertRaisesRegex(
            ControlledEvidenceError,
            "lack required geographic coverage",
        ):
            merge_batches([batch])

    def test_incomplete_or_duplicate_batches_cannot_merge(self) -> None:
        first = batch_from_bundle(0, 16, batch_id="batch-a")
        with self.assertRaisesRegex(ControlledEvidenceError, "are incomplete"):
            merge_batches([first])

        duplicate = batch_from_bundle(0, 16, batch_id="batch-b")
        with self.assertRaisesRegex(
            ControlledEvidenceError,
            "duplicate controlled shadow scenario",
        ):
            batch_status([first, duplicate])

    def test_historical_asin_fallback_can_never_enter_final_bundle(self) -> None:
        batch = batch_from_bundle(0, 33, batch_id="historical-fallback")
        batch["source_evidence"] = {
            "source": "legacy_loopback_mysql",
            "join_strategy": "asin_market_postal_latest",
            "task_identity_sha256": "a" * 64,
            "result_identity_sha256": "b" * 64,
        }

        status = batch_status([batch])

        self.assertTrue(status["matrix_complete"])
        self.assertFalse(status["merge_eligible"])
        self.assertEqual(
            status["diagnostic_only_batch_ids"], ["historical-fallback"]
        )
        with self.assertRaisesRegex(ControlledEvidenceError, "diagnostic only"):
            merge_batches([batch])

    def test_unbound_or_mixed_source_batches_cannot_enter_final_bundle(self) -> None:
        unbound = batch_from_bundle(0, 33, batch_id="unbound")
        unbound["runtime"].pop("v2_source_sha256")

        status = batch_status([unbound])

        self.assertTrue(status["matrix_complete"])
        self.assertFalse(status["source_binding_complete"])
        self.assertFalse(status["merge_eligible"])
        with self.assertRaisesRegex(ControlledEvidenceError, "source fingerprint"):
            merge_batches([unbound])

        first = batch_from_bundle(0, 16, batch_id="source-a")
        second = batch_from_bundle(16, 33, batch_id="source-b")
        second["runtime"]["v2_source_sha256"] = "f" * 64
        with self.assertRaisesRegex(ControlledEvidenceError, "different V2 sources"):
            batch_status([first, second])

    def test_consistently_bound_but_stale_batches_cannot_merge(self) -> None:
        stale = batch_from_bundle(0, 33, batch_id="stale-source")
        stale["runtime"]["v2_source_sha256"] = "f" * 64

        status = batch_status([stale])

        self.assertTrue(status["source_binding_complete"])
        self.assertFalse(status["source_matches_current"])
        self.assertFalse(status["merge_eligible"])
        with self.assertRaisesRegex(ControlledEvidenceError, "stale for the current"):
            merge_batches([stale])


if __name__ == "__main__":
    unittest.main()
