#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import tempfile
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

from amazon_crawler.bootstrap import Application, build_application
from amazon_crawler.config import Settings
from scripts.audit_archived_product_parity import (
    LegacyCollectionParser,
    LegacyMerchantParser,
    LegacyParser,
    _load_current_legacy_collection_parser,
    _load_current_legacy_merchant_parser,
    _load_current_legacy_parser,
)
from scripts.collect_controlled_v2_shadow import (
    _atomic_write_outside_project,
    _digest,
)
from scripts.collect_failure_dual_shadow import (
    DIAGNOSTIC_SCHEMA,
    FAILURE_CASES,
    FailureCandidateMismatch,
    _candidate_diagnostic_report,
    collect_failure_dual_batch,
    validate_failure_dual_plan,
)
from scripts.compile_controlled_evidence import (
    ControlledEvidenceError,
    _scan_for_secret_material,
)
from scripts.merge_controlled_shadow_batches import batch_status


MINIMUM_NETWORK_INTERVAL_SECONDS = 10.0
PREFIX_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")

Collector = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
Persist = Callable[[Path, object], None]
Clock = Callable[[], float]
Sleeper = Callable[[float], Awaitable[None]]
BundleValidator = Callable[[dict[str, Any]], dict[str, Any]]


def validate_campaign_plans(values: list[object]) -> list[dict[str, Any]]:
    if not values:
        raise ControlledEvidenceError("failure campaign requires at least one plan")
    plans: list[dict[str, Any]] = []
    identities: set[tuple[str, str]] = set()
    agreement: dict[str, object] | None = None
    for value in values:
        plan = validate_failure_dual_plan(value)
        if len(plan["scenarios"]) != 1:
            raise ControlledEvidenceError(
                "failure campaign requires exactly one scenario per plan"
            )
        scenario = plan["scenarios"][0]
        identity = (str(scenario["kind"]), str(scenario["case"]))
        if identity in identities:
            raise ControlledEvidenceError(
                f"duplicate failure campaign scenario {identity[0]}/{identity[1]}"
            )
        identities.add(identity)
        current_agreement = {
            "run_id": plan["run_id"],
            "authorization_reference": plan["authorization_reference"],
            "environment": plan["environment"],
            "check_receipts": plan["check_receipts"],
        }
        if agreement is None:
            agreement = current_agreement
        elif current_agreement != agreement:
            raise ControlledEvidenceError(
                "failure campaign plans disagree on run, authorization, environment, "
                "or check receipts"
            )
        plans.append(plan)
    return plans


def load_campaign_plans(paths: list[Path]) -> list[dict[str, Any]]:
    values = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
    return validate_campaign_plans(values)


def _possible_observed_cases(
    plan: dict[str, Any], *, accept_observed_throttle: bool
) -> set[str]:
    planned_case = str(plan["scenarios"][0]["case"])
    cases = {planned_case}
    if planned_case == "no_result" and accept_observed_throttle:
        cases.add("throttle")
    return cases


def prepare_output_targets(
    plans: list[dict[str, Any]],
    *,
    output_dir: Path,
    prefix: str,
    project_root: Path,
    accept_observed_throttle: bool,
    required_observed_case: str | None = None,
) -> tuple[dict[tuple[str, str], Path], dict[tuple[str, str], Path]]:
    if PREFIX_PATTERN.fullmatch(prefix) is None:
        raise ControlledEvidenceError(
            "failure campaign prefix must contain only letters, numbers, dot, dash, "
            "or underscore"
        )
    if output_dir.is_symlink():
        raise ControlledEvidenceError("failure campaign output directory must not be a symlink")
    resolved_dir = output_dir.resolve()
    if resolved_dir.is_relative_to(project_root.resolve()):
        raise ControlledEvidenceError(
            "raw failure campaign artifacts must be written outside the project"
        )
    resolved_dir.mkdir(parents=True, exist_ok=True)
    outputs: dict[tuple[str, str], Path] = {}
    diagnostics: dict[tuple[str, str], Path] = {}
    all_targets: set[Path] = set()
    for plan in plans:
        scenario = plan["scenarios"][0]
        kind = str(scenario["kind"])
        planned_case = str(scenario["case"])
        possible_cases = _possible_observed_cases(
            plan,
            accept_observed_throttle=accept_observed_throttle,
        )
        if required_observed_case is not None:
            if required_observed_case not in FAILURE_CASES:
                raise ControlledEvidenceError(
                    "failure campaign required observed case is invalid"
                )
            if required_observed_case not in possible_cases:
                raise ControlledEvidenceError(
                    f"failure campaign plan {kind}/{planned_case} cannot observe "
                    f"required case {required_observed_case}"
                )
            possible_cases = {required_observed_case}
        for observed_case in possible_cases:
            target = resolved_dir / f"{prefix}-{kind}-{observed_case}.json"
            key = (kind, observed_case)
            if key in outputs or target in all_targets:
                raise ControlledEvidenceError(
                    f"failure campaign output target collides for {kind}/{observed_case}"
                )
            outputs[key] = target
            all_targets.add(target)
        diagnostic = (
            resolved_dir / f"{prefix}-{kind}-{planned_case}-diagnostic.json"
        )
        diagnostics[(kind, planned_case)] = diagnostic
        if diagnostic in all_targets:
            raise ControlledEvidenceError("failure campaign diagnostic target collides")
        all_targets.add(diagnostic)
    for target in all_targets:
        if target.exists() or target.is_symlink():
            raise ControlledEvidenceError(
                f"failure campaign refuses to overwrite existing artifact: {target.name}"
            )
    return outputs, diagnostics


def _validate_single_bundle(bundle: dict[str, Any]) -> dict[str, Any]:
    status = batch_status([bundle])
    if (
        status["scenario_count"] != 1
        or status["passed_scenarios"] != 1
        or status["failed_scenarios"] != 0
    ):
        raise ControlledEvidenceError(
            "failure campaign collector returned a non-passing single-scenario bundle"
        )
    scenarios = bundle.get("scenarios")
    if not isinstance(scenarios, list) or len(scenarios) != 1:
        raise ControlledEvidenceError(
            "failure campaign collector returned an invalid scenario count"
        )
    scenario = scenarios[0]
    return {
        "kind": str(scenario["kind"]),
        "case": str(scenario["case"]),
        "v2_source_sha256": status["v2_source_sha256"],
    }


def _required_case_diagnostic_report(
    plan: dict[str, Any],
    bundle: dict[str, Any],
    *,
    observed: dict[str, Any],
    required_observed_case: str,
) -> dict[str, Any]:
    scenario = bundle["scenarios"][0]
    v2 = scenario.get("v2") if isinstance(scenario, dict) else None
    runtime = bundle.get("runtime")
    report = {
        "schema_version": DIAGNOSTIC_SCHEMA,
        "promotable": False,
        "reason": "required_observed_case_not_seen",
        "batch_id": plan["batch_id"],
        "run_id": plan["run_id"],
        "authorization_reference": plan["authorization_reference"],
        "environment": plan["environment"],
        "observed_at": datetime.now(UTC).isoformat(),
        "diagnostic": {
            "kind": observed["kind"],
            "expected_case": required_observed_case,
            "planned_case": plan["scenarios"][0]["case"],
            "input_sha256": _digest(plan["scenarios"][0]["input"]),
            "response_set_sha256": (
                v2.get("response_set_sha256") if isinstance(v2, dict) else None
            ),
            "v2_source_sha256": (
                runtime.get("v2_source_sha256")
                if isinstance(runtime, dict)
                else None
            ),
            "observed": {
                "status": "validated_failure",
                "case": observed["case"],
                "error_code": v2.get("error_code")
                if isinstance(v2, dict)
                else None,
                "retryable": v2.get("retryable")
                if isinstance(v2, dict)
                else None,
            },
        },
    }
    _scan_for_secret_material(report, path="candidate_diagnostic_report")
    return report


async def run_failure_campaign(
    raw_plans: list[object],
    *,
    collector: Collector,
    outputs: dict[tuple[str, str], Path],
    diagnostics: dict[tuple[str, str], Path],
    minimum_interval_seconds: float,
    persist: Persist,
    required_observed_case: str | None = None,
    clock: Clock = time.monotonic,
    sleep: Sleeper = asyncio.sleep,
    bundle_validator: BundleValidator = _validate_single_bundle,
) -> dict[str, Any]:
    plans = validate_campaign_plans(raw_plans)
    if minimum_interval_seconds < MINIMUM_NETWORK_INTERVAL_SECONDS:
        raise ControlledEvidenceError(
            "failure campaign network interval must be at least ten seconds"
        )
    rows: list[dict[str, Any]] = []
    last_started_at: float | None = None
    source_hashes: set[str] = set()
    for plan in plans:
        scenario = plan["scenarios"][0]
        kind = str(scenario["kind"])
        planned_case = str(scenario["case"])
        if last_started_at is not None:
            remaining = minimum_interval_seconds - (clock() - last_started_at)
            if remaining > 0:
                await sleep(remaining)
        last_started_at = clock()
        try:
            bundle = await collector(plan)
            observed = bundle_validator(bundle)
            if observed["kind"] != kind:
                raise ControlledEvidenceError(
                    "failure campaign collector changed the scenario kind"
                )
            source_hash = observed.get("v2_source_sha256")
            if not isinstance(source_hash, str) or not source_hash:
                raise ControlledEvidenceError(
                    "failure campaign bundle is not bound to V2 source"
                )
            source_hashes.add(source_hash)
            if len(source_hashes) > 1:
                raise ControlledEvidenceError(
                    "failure campaign bundles were produced from different V2 sources"
                )
            if (
                required_observed_case is not None
                and observed["case"] != required_observed_case
            ):
                target = diagnostics[(kind, planned_case)]
                persist(
                    target,
                    _required_case_diagnostic_report(
                        plan,
                        bundle,
                        observed=observed,
                        required_observed_case=required_observed_case,
                    ),
                )
                rows.append(
                    {
                        "kind": kind,
                        "planned_case": planned_case,
                        "observed_case": observed["case"],
                        "status": "candidate_mismatch",
                        "diagnostic_output": str(target),
                    }
                )
                continue
            target = outputs.get((kind, observed["case"]))
            if target is None:
                raise ControlledEvidenceError(
                    "failure campaign observed a case without a preflighted target"
                )
            persist(target, bundle)
            rows.append(
                {
                    "kind": kind,
                    "planned_case": planned_case,
                    "observed_case": observed["case"],
                    "status": "persisted",
                    "output": str(target),
                }
            )
        except FailureCandidateMismatch as exc:
            target = diagnostics[(kind, planned_case)]
            persist(target, _candidate_diagnostic_report(plan, exc))
            rows.append(
                {
                    "kind": kind,
                    "planned_case": planned_case,
                    "observed_case": None,
                    "status": "candidate_mismatch",
                    "diagnostic_output": str(target),
                }
            )
    persisted_count = sum(row["status"] == "persisted" for row in rows)
    return {
        "schema_version": "controlled-failure-campaign-summary.v1",
        "ok": persisted_count == len(plans),
        "attempted_count": len(rows),
        "persisted_count": persisted_count,
        "candidate_mismatch_count": sum(
            row["status"] == "candidate_mismatch" for row in rows
        ),
        "strictly_serial": True,
        "minimum_interval_seconds": minimum_interval_seconds,
        "v2_source_sha256": next(iter(source_hashes), None),
        "results": rows,
    }


def _build_collector(
    *,
    application: Application,
    product_parser: LegacyParser,
    collection_parser: LegacyCollectionParser,
    merchant_parser: LegacyMerchantParser,
    legacy_root: Path,
    expected_authorization_reference: str | None,
    accept_observed_throttle: bool,
) -> Collector:
    async def collect(plan: dict[str, Any]) -> dict[str, Any]:
        return await collect_failure_dual_batch(
            plan,
            application=application,
            product_parser=product_parser,
            collection_parser=collection_parser,
            merchant_parser=merchant_parser,
            legacy_source_root=legacy_root,
            expected_authorization_reference=expected_authorization_reference,
            accept_observed_throttle=accept_observed_throttle,
        )

    return collect


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run authorized failure candidates strictly serially, with a hard "
            "minimum interval and independent artifacts. The runner never retries "
            "a candidate and never attempts to induce throttling."
        )
    )
    parser.add_argument("plans", nargs="+", type=Path)
    parser.add_argument("--legacy-source-root", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--prefix", required=True)
    parser.add_argument(
        "--minimum-interval-seconds",
        type=float,
        default=15.0,
    )
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--confirm-authorized-network", action="store_true")
    parser.add_argument("--accept-observed-throttle", action="store_true")
    parser.add_argument(
        "--require-observed-case",
        choices=sorted(FAILURE_CASES),
        help=(
            "persist a promotable bundle only when the validated observed case "
            "matches this value"
        ),
    )
    args = parser.parse_args()
    project_root = Path(__file__).resolve().parents[1]
    try:
        plans = load_campaign_plans(args.plans)
        outputs, diagnostics = prepare_output_targets(
            plans,
            output_dir=args.output_dir,
            prefix=args.prefix,
            project_root=project_root,
            accept_observed_throttle=args.accept_observed_throttle,
            required_observed_case=args.require_observed_case,
        )
        if args.validate_only:
            print(
                json.dumps(
                    {
                        "ok": True,
                        "validated_only": True,
                        "scenario_count": len(plans),
                        "strictly_serial": True,
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
                "failure campaign requires explicit network confirmation"
            )
        if args.minimum_interval_seconds < MINIMUM_NETWORK_INTERVAL_SECONDS:
            raise ControlledEvidenceError(
                "failure campaign network interval must be at least ten seconds"
            )
        legacy_root = args.legacy_source_root.resolve()
        product_parser = _load_current_legacy_parser(legacy_root)
        collection_parser = _load_current_legacy_collection_parser(legacy_root)
        merchant_parser = _load_current_legacy_merchant_parser(legacy_root)
        with tempfile.TemporaryDirectory(prefix="amazon-crawler-failure-campaign-") as tempdir:
            temporary_root = Path(tempdir)
            settings = replace(
                Settings.from_env(project_root),
                db_path=temporary_root / "crawler.db",
                evidence_dir=temporary_root / "evidence",
                result_jsonl_dir=temporary_root / "results",
                capture_evidence=False,
                worker_enabled=False,
                delivery_worker_enabled=False,
                cookie_maintenance_enabled=False,
            )
            application = build_application(settings)
            collector = _build_collector(
                application=application,
                product_parser=product_parser,
                collection_parser=collection_parser,
                merchant_parser=merchant_parser,
                legacy_root=legacy_root,
                expected_authorization_reference=os.getenv(
                    "CONTROLLED_ACCEPTANCE_AUTHORIZATION"
                ),
                accept_observed_throttle=args.accept_observed_throttle,
            )

            def persist(path: Path, payload: object) -> None:
                _atomic_write_outside_project(
                    path,
                    payload,
                    project_root=project_root,
                )

            summary = asyncio.run(
                run_failure_campaign(
                    plans,
                    collector=collector,
                    outputs=outputs,
                    diagnostics=diagnostics,
                    minimum_interval_seconds=args.minimum_interval_seconds,
                    persist=persist,
                    required_observed_case=args.require_observed_case,
                )
            )
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
            else "controlled failure campaign failed"
        )
        print(
            json.dumps(
                {"ok": False, "error": {"type": type(exc).__name__, "message": message}},
                ensure_ascii=False,
            )
        )
        return 2
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if summary["ok"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
