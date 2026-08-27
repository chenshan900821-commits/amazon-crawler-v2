#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import tempfile
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from amazon_crawler.bootstrap import Application, build_application
from amazon_crawler.config import Settings
from amazon_crawler.domain.errors import CrawlerError
from amazon_crawler.domain.models import ClaimedItem, CrawlFailure, ExecutionMode
from amazon_crawler.infra.legacy_compat import LEGACY_MAX_ATTEMPTS
from amazon_crawler.plugins.amazon_parser import (
    LEGACY_DIMENSION_FIELDS,
    LEGACY_PRODUCT_FIELDS,
)
from amazon_crawler.plugins.amazon_product import AmazonProductPlugin
from amazon_crawler.plugins.marketplaces import MARKETPLACES
from scripts.audit_archived_product_parity import (
    LegacyParser,
    _file_digest,
    _load_current_legacy_parser,
)
from scripts.collect_controlled_v2_shadow import (
    BATCH_BUNDLE_SCHEMA,
    EXTERNALLY_PROVEN_CHECKS,
    _atomic_write_outside_project,
    _digest,
    _missing_scenarios,
    _validate_external_check_receipts,
    validate_runtime_prerequisites,
)
from scripts.compile_controlled_evidence import (
    ControlledEvidenceError,
    _is_sha256,
    _scan_for_secret_material,
)
from scripts.runtime_source_fingerprint import runtime_source_sha256


PLAN_SCHEMA = "controlled-product-dual-plan.v1"
PRODUCT_KINDS = {"product", "product_hw", "product_time"}
EXECUTION_MODES = {
    "product_hw": ExecutionMode.OVERSEAS,
    "product_time": ExecutionMode.REALTIME,
}
LEGACY_TASKS = {
    "product": "product_jp",
    "product_hw": "product_hw_jp",
    "product_time": "product_time_jp",
}


class RecordingFetcher:
    """Keep successful response objects in memory without persisting HTML."""

    def __init__(self, delegate: Any) -> None:
        self._delegate = delegate
        self._responses: list[tuple[int, Any]] = []
        self._sequence = 0

    @property
    def responses(self) -> list[Any]:
        return [response for _, response in sorted(self._responses)]

    @property
    def backend(self) -> str:
        return str(self._delegate.backend)

    @property
    def tls_impersonation(self) -> bool:
        return bool(self._delegate.tls_impersonation)

    async def fetch(self, *args: Any, **kwargs: Any) -> Any:
        sequence = self._sequence
        self._sequence += 1
        observed: list[Any] = []
        if getattr(self._delegate, "supports_response_observer", False):
            kwargs["response_observer"] = observed.append
        response = await self._delegate.fetch(*args, **kwargs)
        if observed:
            self._responses.append((sequence, observed[-1]))
        elif not isinstance(response, CrawlFailure):
            self._responses.append((sequence, response))
        return response

    async def report(self, *args: Any, **kwargs: Any) -> Any:
        return await self._delegate.report(*args, **kwargs)


def _legacy_source_hash(legacy_source_root: Path) -> str:
    return _digest(
        {
            "product_parser": _file_digest(
                legacy_source_root / "tools/product_parser_utils.py"
            ),
            "parser_helpers": _file_digest(legacy_source_root / "tools/lxml_tool.py"),
            "parser_config": _file_digest(legacy_source_root / "settings/config.py"),
        }
    )


def validate_product_dual_plan(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ControlledEvidenceError("product dual plan must be an object")
    _scan_for_secret_material(value, path="plan")
    if value.get("schema_version") != PLAN_SCHEMA:
        raise ControlledEvidenceError(f"schema_version must be {PLAN_SCHEMA}")
    if value.get("environment") != "isolated":
        raise ControlledEvidenceError("product dual plan environment must be isolated")
    for field in ("batch_id", "run_id", "authorization_reference"):
        if not isinstance(value.get(field), str) or not value[field].strip():
            raise ControlledEvidenceError(f"product dual plan {field} is required")
    _validate_external_check_receipts(value)
    scenarios = value.get("scenarios")
    if not isinstance(scenarios, list) or not 1 <= len(scenarios) <= len(PRODUCT_KINDS):
        raise ControlledEvidenceError(
            "product dual plan requires between one and three scenarios"
        )
    identities: set[tuple[str, str]] = set()
    for index, scenario in enumerate(scenarios):
        if not isinstance(scenario, dict):
            raise ControlledEvidenceError(f"product dual scenario {index} is invalid")
        kind = scenario.get("kind")
        case = scenario.get("case")
        identity = (str(kind), str(case))
        if kind not in PRODUCT_KINDS or case != "success":
            raise ControlledEvidenceError(
                "product dual plans currently accept only product success scenarios"
            )
        if identity in identities:
            raise ControlledEvidenceError(f"duplicate product dual scenario {kind}/{case}")
        identities.add(identity)
        if scenario.get("authorized") is not True:
            raise ControlledEvidenceError(f"scenario {kind}/{case} is not authorized")
        if not isinstance(scenario.get("marketplace_id"), str) or not scenario[
            "marketplace_id"
        ].strip():
            raise ControlledEvidenceError(f"scenario {kind}/{case} needs marketplace_id")
        if not isinstance(scenario.get("postal_code"), str) or not scenario[
            "postal_code"
        ].strip():
            raise ControlledEvidenceError(f"scenario {kind}/{case} needs postal_code")
        if not isinstance(scenario.get("input"), dict) or not scenario["input"]:
            raise ControlledEvidenceError(f"scenario {kind}/{case} needs input")
        if "legacy" in scenario:
            raise ControlledEvidenceError(
                "product dual plan must not carry a precomputed legacy result"
            )
    return value


def _legacy_projection(
    *,
    kind: str,
    task_id: str,
    result: dict[str, Any],
    dimensions: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    current = dict(result)
    current["task_id"] = task_id
    product_row = {field: current.get(field) for field in LEGACY_PRODUCT_FIELDS}
    dimension_rows: list[dict[str, Any]] = []
    if kind != "product_time":
        dimension_rows = [
            {
                field: row.get(field)
                for field in LEGACY_DIMENSION_FIELDS
            }
            for row in dimensions
        ]
    return {
        "result_rows": [product_row],
        "dimension_rows": dimension_rows,
        "child_task_rows": [],
        "product_task_rows": [],
    }


async def _execute_scenario(
    application: Application,
    scenario: dict[str, Any],
    *,
    legacy_parser: LegacyParser,
    legacy_source_sha256: str,
) -> dict[str, Any]:
    kind = str(scenario["kind"])
    template = application.plugins.get(kind)
    recording = RecordingFetcher(application.fetcher)
    plugin = AmazonProductPlugin(
        fetcher=recording,  # type: ignore[arg-type]
        evidence_store=template._evidence_store,  # type: ignore[attr-defined]
        require_cookie=template._require_cookie,  # type: ignore[attr-defined]
        kind=kind,
    )
    try:
        normalized = plugin.normalize(
            scenario["input"],
            str(scenario["marketplace_id"]),
            str(scenario["postal_code"]),
        )
    except (CrawlerError, TypeError, ValueError) as exc:
        raise ControlledEvidenceError(
            f"product dual scenario {kind}/success input is invalid"
        ) from exc
    identity = _digest(
        {
            "run_id": scenario["run_id"],
            "kind": kind,
            "case": "success",
            "input": normalized.as_dict(),
        }
    )[:24]
    task_id = str(normalized.as_dict().get("id") or f"controlled-{identity}")
    item = ClaimedItem(
        id=f"controlled-item-{identity}",
        job_id=f"controlled-job-{identity}",
        seq=1,
        kind=kind,
        execution_mode=EXECUTION_MODES.get(kind, ExecutionMode.STANDARD),
        input=normalized.as_dict(),
        options={},
        attempts=1,
        max_attempts=LEGACY_MAX_ATTEMPTS.get(LEGACY_TASKS[kind], 5),
        lease_owner="controlled-product-dual",
        lease_token=f"controlled-product-dual-{identity}",
    )
    outcome = await plugin.execute(item)
    if not outcome.ok or outcome.result is None:
        code = outcome.failure.code if outcome.failure is not None else "unknown"
        raise ControlledEvidenceError(
            f"product dual scenario {kind}/success did not succeed: {code}"
        )
    if len(recording.responses) != 1:
        raise ControlledEvidenceError(
            f"product dual scenario {kind}/success did not capture exactly one response"
        )
    fetched = recording.responses[0]
    response_sha256 = outcome.result.evidence.get("sha256")
    captured_sha256 = hashlib.sha256(fetched.html.encode("utf-8")).hexdigest()
    if not _is_sha256(response_sha256) or response_sha256 != captured_sha256:
        raise ControlledEvidenceError(
            f"product dual scenario {kind}/success response evidence does not match"
        )
    market = MARKETPLACES[normalized.marketplace_id]
    try:
        legacy_dimensions, legacy_result = legacy_parser(
            fetched.html,
            {
                "id": task_id,
                "market_id": market.amazon_marketplace_id,
                "asin": str(normalized.as_dict()["asin"]),
                "post_code": str(normalized.postal_code or ""),
            },
            int(fetched.status_code),
        )
    except Exception as exc:
        raise ControlledEvidenceError(
            f"current legacy parser rejected product dual scenario {kind}/success "
            f"with {type(exc).__name__}"
        ) from exc
    if not isinstance(legacy_result, dict) or not isinstance(legacy_dimensions, list):
        raise ControlledEvidenceError("current legacy parser returned an invalid result")
    projection = _legacy_projection(
        kind=kind,
        task_id=task_id,
        result=legacy_result,
        dimensions=legacy_dimensions,
    )
    evidence = {
        "source": "current_legacy_source",
        "join_strategy": "same_response",
        "legacy_source_sha256": legacy_source_sha256,
        "response_sha256": str(response_sha256),
    }
    return {
        "kind": kind,
        "case": "success",
        "authorized": True,
        "marketplace_id": normalized.marketplace_id,
        "postal_code": normalized.postal_code,
        "input": normalized.as_dict(),
        "legacy": {
            "state": 1,
            "projection": projection,
            "evidence": evidence,
        },
        "v2": {
            "status": "succeeded",
            "error_code": None,
            "retryable": False,
            "response_sha256": response_sha256,
            "result": {
                "schema_version": outcome.result.schema_version,
                "data": outcome.result.data,
                "evidence": outcome.result.evidence,
            },
        },
    }


async def collect_product_dual_batch(
    plan: object,
    *,
    application: Application,
    legacy_parser: LegacyParser,
    legacy_source_sha256: str,
    expected_authorization_reference: str | None,
) -> dict[str, Any]:
    plan = validate_product_dual_plan(plan)
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
                legacy_parser=legacy_parser,
                legacy_source_sha256=legacy_source_sha256,
            )
        )
    validated_at = datetime.now(UTC).isoformat()
    check_receipts = _validate_external_check_receipts(plan)
    response_hashes = [scenario["v2"]["response_sha256"] for scenario in scenarios]
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
                "response_hashes": response_hashes,
            }
        ),
    }
    identities = {(scenario["kind"], scenario["case"]) for scenario in scenarios}
    missing = _missing_scenarios(identities)
    projections = [scenario["legacy"]["projection"] for scenario in scenarios]
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
            "result_identity_sha256": _digest(projections),
            "legacy_source_sha256": legacy_source_sha256,
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
            "Fetch authorized product pages once, then run the current legacy "
            "parser and V2 parser against the exact same in-memory response."
        )
    )
    parser.add_argument("plan", type=Path)
    parser.add_argument("--legacy-source-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--confirm-authorized-network", action="store_true")
    args = parser.parse_args()
    project_root = Path(__file__).resolve().parents[1]
    try:
        plan = validate_product_dual_plan(
            json.loads(args.plan.read_text(encoding="utf-8"))
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
                "product dual collection requires explicit network confirmation"
            )
        legacy_source_root = args.legacy_source_root.resolve()
        legacy_parser = _load_current_legacy_parser(legacy_source_root)
        legacy_source_sha256 = _legacy_source_hash(legacy_source_root)
        with tempfile.TemporaryDirectory(prefix="amazon-crawler-product-dual-") as tempdir:
            settings = replace(
                Settings.from_env(project_root),
                db_path=Path(tempdir) / "crawler.db",
                evidence_dir=Path(tempdir) / "evidence",
                capture_evidence=False,
                worker_enabled=False,
            )
            application = build_application(settings)
            bundle = asyncio.run(
                collect_product_dual_batch(
                    plan,
                    application=application,
                    legacy_parser=legacy_parser,
                    legacy_source_sha256=legacy_source_sha256,
                    expected_authorization_reference=os.getenv(
                        "CONTROLLED_ACCEPTANCE_AUTHORIZATION"
                    ),
                )
            )
        _atomic_write_outside_project(
            args.output,
            bundle,
            project_root=project_root,
        )
        from scripts.merge_controlled_shadow_batches import batch_status

        # Report the independently re-read artifact, not only in-memory state.
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
        message = (
            str(exc)
            if isinstance(exc, ControlledEvidenceError)
            else "product dual shadow collection failed"
        )
        print(
            json.dumps(
                {"ok": False, "error": {"type": type(exc).__name__, "message": message}},
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
