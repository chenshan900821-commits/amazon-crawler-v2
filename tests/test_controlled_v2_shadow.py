from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from amazon_crawler.domain.models import (
    CrawlFailure,
    CrawlResult,
    NormalizedInput,
    PluginOutcome,
)
from scripts.build_controlled_run_report import _normalize_input, build_run_report
from scripts.collect_controlled_v2_shadow import (
    _atomic_write_outside_project,
    collect_shadow_bundle,
    main,
    validate_runtime_prerequisites,
    validate_shadow_plan,
)
from scripts.compile_controlled_evidence import (
    SCENARIO_CASES,
    ControlledEvidenceError,
    validate_run_report,
)
from scripts.verify_parity_manifest import EXPECTED_TASKS
from tests.test_controlled_run_builder import (
    NO_RESULT_CODES,
    success_result,
    valid_bundle,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def valid_plan() -> dict[str, object]:
    bundle = valid_bundle()
    scenarios = []
    for scenario in bundle["scenarios"]:
        raw_input = dict(scenario["input"])
        raw_input["add_date"] = scenario["case"]
        scenarios.append(
            {
                "kind": scenario["kind"],
                "case": scenario["case"],
                "authorized": True,
                "marketplace_id": scenario["marketplace_id"],
                "postal_code": scenario["postal_code"],
                "input": raw_input,
                "legacy": scenario["legacy"],
            }
        )
    return {
        "schema_version": "controlled-shadow-plan.v1",
        "run_id": bundle["run_id"],
        "authorization_reference": bundle["authorization_reference"],
        "environment": "isolated",
        "check_receipts": {
            key: value
            for key, value in bundle["check_receipts"].items()
            if key != "tls_impersonation"
        },
        "scenarios": scenarios,
    }


class FakePlugin:
    def __init__(self, kind: str, *, response_less: bool = False) -> None:
        self.kind = kind
        self.response_less = response_less

    def normalize(self, value, marketplace_id, postal_code):
        payload, market, post = _normalize_input(
            self.kind,
            value,
            marketplace_id=marketplace_id,
            postal_code=postal_code,
        )
        return NormalizedInput.from_payload(
            payload,
            input_key=f"controlled:{self.kind}:{payload.get('add_date')}",
            marketplace_id=market,
            postal_code=post,
            source=value,
        )

    async def execute(self, item):
        case = item.input["add_date"]
        response_sha = hashlib.sha256(
            f"response:{self.kind}:{case}".encode("utf-8")
        ).hexdigest()
        if case == "success":
            result = success_result(self.kind, response_sha)
            return PluginOutcome(
                result=CrawlResult(
                    data=result["data"],
                    evidence=result["evidence"],
                    schema_version="controlled.fixture.v1",
                )
            )
        code = (
            NO_RESULT_CODES[self.kind]
            if case == "no_result"
            else "upstream_retryable"
        )
        details = {} if self.response_less else {"evidence": {"sha256": response_sha}}
        return PluginOutcome(
            failure=CrawlFailure(
                code,
                "controlled fixture failure",
                case == "throttle",
                details,
            )
        )


class FakeRegistry:
    def __init__(self, *, response_less: bool = False) -> None:
        self.response_less = response_less

    def get(self, kind: str) -> FakePlugin:
        return FakePlugin(kind, response_less=self.response_less)


def fake_application(*, response_less: bool = False, all_resources: bool = True):
    configured = "configured" if all_resources else None
    return SimpleNamespace(
        settings=SimpleNamespace(
            require_cookie=True,
            cookie_redis_url=configured,
            cookie_redis_overseas_url=configured,
            merchant_cookie=configured,
            proxy_extract_url=configured,
        ),
        fetcher=SimpleNamespace(backend="curl_cffi", tls_impersonation=True),
        plugins=FakeRegistry(response_less=response_less),
    )


class ControlledV2ShadowTests(unittest.IsolatedAsyncioTestCase):
    def test_published_plan_schema_stays_in_sync_with_runtime_matrix(self) -> None:
        schema = json.loads(
            (
                PROJECT_ROOT
                / "contracts"
                / "controlled-shadow-plan.schema.json"
            ).read_text(encoding="utf-8")
        )
        scenario = schema["$defs"]["scenario"]["properties"]

        self.assertEqual(set(scenario["kind"]["enum"]), EXPECTED_TASKS)
        self.assertEqual(set(scenario["case"]["enum"]), SCENARIO_CASES)
        self.assertEqual(schema["properties"]["scenarios"]["minItems"], 33)
        self.assertEqual(schema["properties"]["scenarios"]["maxItems"], 33)

    def test_validate_only_never_loads_runtime_or_sends_network_requests(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            plan_path = Path(tempdir) / "plan.json"
            plan_path.write_text(
                json.dumps(valid_plan()),
                encoding="utf-8",
            )
            output = io.StringIO()
            with (
                patch(
                    "sys.argv",
                    [
                        "collect-controlled-v2-shadow",
                        str(plan_path),
                        "--validate-only",
                    ],
                ),
                patch(
                    "scripts.collect_controlled_v2_shadow.build_application"
                ) as build_application,
                redirect_stdout(output),
            ):
                exit_code = main()

        self.assertEqual(exit_code, 0)
        build_application.assert_not_called()
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["scenario_count"], 33)
        self.assertEqual(payload["network_requests"], 0)

    async def test_collector_executes_matrix_and_builds_a_comparable_bundle(self) -> None:
        plan = valid_plan()

        bundle = await collect_shadow_bundle(
            plan,
            application=fake_application(),
            expected_authorization_reference=plan["authorization_reference"],
            concurrency=3,
        )

        self.assertEqual(len(bundle["scenarios"]), 33)
        self.assertEqual(
            bundle["check_receipts"]["tls_impersonation"]["status"],
            "passed",
        )
        report = build_run_report(bundle)
        self.assertIs(validate_run_report(report), report)

    async def test_collector_rejects_response_less_failures(self) -> None:
        plan = valid_plan()

        with self.assertRaisesRegex(
            ControlledEvidenceError,
            "did not receive an upstream response",
        ):
            await collect_shadow_bundle(
                plan,
                application=fake_application(response_less=True),
                expected_authorization_reference=plan["authorization_reference"],
            )

    def test_authorization_and_isolated_resource_preflight_are_mandatory(self) -> None:
        plan = validate_shadow_plan(valid_plan())
        with self.assertRaisesRegex(
            ControlledEvidenceError,
            "must match the plan",
        ):
            validate_runtime_prerequisites(
                application=fake_application(),
                authorization_reference=plan["authorization_reference"],
                expected_authorization_reference=None,
            )
        with self.assertRaisesRegex(
            ControlledEvidenceError,
            "missing required isolated resources",
        ):
            validate_runtime_prerequisites(
                application=fake_application(all_resources=False),
                authorization_reference=plan["authorization_reference"],
                expected_authorization_reference=plan["authorization_reference"],
            )

    def test_raw_bundle_cannot_be_written_inside_the_project(self) -> None:
        with self.assertRaisesRegex(
            ControlledEvidenceError,
            "outside the project",
        ):
            _atomic_write_outside_project(
                PROJECT_ROOT / "evidence" / "raw-shadow.json",
                {"raw": "business-data"},
                project_root=PROJECT_ROOT,
            )

        with tempfile.TemporaryDirectory() as tempdir:
            output = Path(tempdir) / "raw-shadow.json"
            _atomic_write_outside_project(
                output,
                {"raw": "business-data"},
                project_root=PROJECT_ROOT,
            )
            self.assertTrue(output.is_file())


if __name__ == "__main__":
    unittest.main()
