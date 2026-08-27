from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from scripts.build_failure_dual_plan import build_plan
from scripts.collect_failure_dual_shadow import FailureCandidateMismatch
from scripts.compile_controlled_evidence import ControlledEvidenceError
from scripts.convert_product_shadow_plan_to_dual import convert_plan
from scripts.run_controlled_failure_campaign import (
    _required_case_diagnostic_report,
    prepare_output_targets,
    run_failure_campaign,
    validate_campaign_plans,
)
from tests.test_controlled_v2_shadow import valid_plan


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def campaign_plan(kind: str, *, case: str = "throttle") -> dict[str, object]:
    source = convert_plan(
        valid_plan(),
        batch_id=f"source-{kind}",
        kinds=["product"],
    )
    return build_plan(
        source,
        batch_id=f"campaign-{kind}-{case}",
        kinds=[kind],
        case=case,
        seller_id="ACSFBZX3I4JAS",
        postal_code=None,
    )


def observed_bundle(kind: str, case: str) -> dict[str, object]:
    return {"scenarios": [{"kind": kind, "case": case}]}


def validate_observed(bundle: dict[str, object]) -> dict[str, object]:
    scenario = bundle["scenarios"][0]  # type: ignore[index]
    return {
        "kind": scenario["kind"],  # type: ignore[index]
        "case": scenario["case"],  # type: ignore[index]
        "v2_source_sha256": "a" * 64,
    }


class FakeTime:
    def __init__(self) -> None:
        self.value = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.value

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.value += seconds


class ControlledFailureCampaignTests(unittest.IsolatedAsyncioTestCase):
    def test_validation_rejects_multi_scenario_and_duplicate_identity(self) -> None:
        product = campaign_plan("product")
        duplicate = copy.deepcopy(product)
        duplicate["batch_id"] = "duplicate-product"
        with self.assertRaisesRegex(
            ControlledEvidenceError,
            "duplicate failure campaign scenario product/throttle",
        ):
            validate_campaign_plans([product, duplicate])

        multi = copy.deepcopy(product)
        multi["scenarios"].append(copy.deepcopy(multi["scenarios"][0]))
        multi["scenarios"][1]["kind"] = "product_hw"
        with self.assertRaisesRegex(
            ControlledEvidenceError,
            "exactly one scenario per plan",
        ):
            validate_campaign_plans([multi])

    def test_output_collision_is_rejected_before_collection(self) -> None:
        plan = campaign_plan("product")
        with tempfile.TemporaryDirectory() as tempdir:
            output_dir = Path(tempdir) / "evidence"
            output_dir.mkdir()
            (output_dir / "round-product-throttle.json").write_text(
                "already here", encoding="utf-8"
            )
            with self.assertRaisesRegex(
                ControlledEvidenceError,
                "refuses to overwrite existing artifact",
            ):
                prepare_output_targets(
                    [plan],
                    output_dir=output_dir,
                    prefix="round",
                    project_root=PROJECT_ROOT,
                    accept_observed_throttle=False,
                )

    async def test_campaign_is_serial_and_persists_around_a_candidate_miss(self) -> None:
        plans = [
            campaign_plan("product"),
            campaign_plan("reviews"),
            campaign_plan("merchant"),
        ]
        sentinel = "cookie=session-secret-must-not-leak"
        plans[1]["scenarios"][0]["input"]["sentinel"] = sentinel
        starts: list[float] = []
        fake_time = FakeTime()
        persisted: list[tuple[str, object]] = []

        async def collect(plan):
            starts.append(fake_time.clock())
            kind = plan["scenarios"][0]["kind"]
            if kind == "reviews":
                raise FailureCandidateMismatch(
                    "candidate missed",
                    {
                        "kind": "reviews",
                        "expected_case": "throttle",
                        "input_sha256": "b" * 64,
                        "response_count": 0,
                        "responses": [],
                        "response_set_sha256": None,
                        "observed": {"status": "failed", "error_code": "network_error"},
                    },
                )
            return observed_bundle(kind, "throttle")

        def persist(path: Path, payload: object) -> None:
            persisted.append((path.name, payload))

        outputs = {
            (kind, "throttle"): Path(f"/safe/round-{kind}-throttle.json")
            for kind in ("product", "reviews", "merchant")
        }
        diagnostics = {
            (kind, "throttle"): Path(f"/safe/round-{kind}-diagnostic.json")
            for kind in ("product", "reviews", "merchant")
        }
        summary = await run_failure_campaign(
            plans,
            collector=collect,
            outputs=outputs,
            diagnostics=diagnostics,
            minimum_interval_seconds=10.0,
            persist=persist,
            clock=fake_time.clock,
            sleep=fake_time.sleep,
            bundle_validator=validate_observed,
        )

        self.assertEqual(starts, [0.0, 10.0, 20.0])
        self.assertEqual(fake_time.sleeps, [10.0, 10.0])
        self.assertEqual(summary["persisted_count"], 2)
        self.assertEqual(summary["candidate_mismatch_count"], 1)
        self.assertEqual(len(persisted), 3)
        self.assertNotIn(sentinel, json.dumps(summary, sort_keys=True))

    async def test_every_plan_is_validated_before_the_first_collection(self) -> None:
        valid = campaign_plan("product")
        invalid = copy.deepcopy(campaign_plan("reviews"))
        invalid["scenarios"][0]["authorized"] = False
        calls = 0

        async def collect(plan):
            nonlocal calls
            calls += 1
            return observed_bundle("product", "throttle")

        with self.assertRaisesRegex(ControlledEvidenceError, "is not authorized"):
            await run_failure_campaign(
                [valid, invalid],
                collector=collect,
                outputs={},
                diagnostics={},
                minimum_interval_seconds=10.0,
                persist=lambda path, payload: None,
                bundle_validator=validate_observed,
            )
        self.assertEqual(calls, 0)

    async def test_required_throttle_does_not_persist_a_valid_no_result_bundle(self) -> None:
        plan = campaign_plan("product", case="no_result")
        sentinel = "cookie=session-secret-must-not-leak"
        plan["scenarios"][0]["input"]["sentinel"] = sentinel
        persisted: list[tuple[str, object]] = []

        async def collect(value):
            return {
                "runtime": {"v2_source_sha256": "a" * 64},
                "scenarios": [
                    {
                        "kind": "product",
                        "case": "no_result",
                        "v2": {
                            "error_code": "product_not_found",
                            "retryable": False,
                            "response_set_sha256": "b" * 64,
                        },
                    }
                ],
            }

        summary = await run_failure_campaign(
            [plan],
            collector=collect,
            outputs={
                ("product", "throttle"): Path("/safe/round-product-throttle.json")
            },
            diagnostics={
                ("product", "no_result"): Path("/safe/round-product-diagnostic.json")
            },
            minimum_interval_seconds=10.0,
            persist=lambda path, payload: persisted.append((path.name, payload)),
            required_observed_case="throttle",
            bundle_validator=validate_observed,
        )

        self.assertEqual(summary["persisted_count"], 0)
        self.assertEqual(summary["candidate_mismatch_count"], 1)
        self.assertEqual(len(persisted), 1)
        self.assertEqual(
            persisted[0][1]["reason"],
            "required_observed_case_not_seen",
        )
        self.assertNotIn(sentinel, json.dumps(persisted[0][1], sort_keys=True))

    def test_required_case_diagnostic_contains_only_hashes_and_safe_state(self) -> None:
        plan = campaign_plan("reviews", case="no_result")
        sentinel = "proxy-password-secret"
        plan["scenarios"][0]["input"]["private"] = sentinel
        report = _required_case_diagnostic_report(
            plan,
            {
                "runtime": {"v2_source_sha256": "a" * 64},
                "scenarios": [
                    {
                        "kind": "reviews",
                        "case": "no_result",
                        "v2": {
                            "error_code": "no_reviews",
                            "retryable": False,
                            "response_set_sha256": "b" * 64,
                        },
                    }
                ],
            },
            observed={
                "kind": "reviews",
                "case": "no_result",
                "v2_source_sha256": "a" * 64,
            },
            required_observed_case="throttle",
        )

        rendered = json.dumps(report, sort_keys=True)
        self.assertNotIn(sentinel, rendered)
        self.assertEqual(report["diagnostic"]["observed"]["case"], "no_result")


if __name__ == "__main__":
    unittest.main()
