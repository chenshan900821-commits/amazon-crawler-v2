#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from scripts.collect_controlled_v2_shadow import (
    _atomic_write_outside_project,
    validate_shadow_batch_plan,
    validate_shadow_plan,
)
from scripts.collect_product_dual_shadow import (
    PLAN_SCHEMA,
    PRODUCT_KINDS,
    validate_product_dual_plan,
)
from scripts.compile_controlled_evidence import ControlledEvidenceError


def convert_plan(
    source: object,
    *,
    batch_id: str,
    kinds: list[str],
) -> dict[str, Any]:
    if not isinstance(source, dict):
        raise ControlledEvidenceError("source shadow plan must be an object")
    if source.get("schema_version") == "controlled-shadow-batch-plan.v1":
        source = validate_shadow_batch_plan(source)
    else:
        source = validate_shadow_plan(source)
    unsupported = sorted(set(kinds) - PRODUCT_KINDS)
    if unsupported or not kinds or len(set(kinds)) != len(kinds):
        raise ControlledEvidenceError("dual product kinds must be unique and supported")
    seed = next(
        (
            scenario
            for scenario in source["scenarios"]
            if scenario.get("kind") in PRODUCT_KINDS
            and scenario.get("case") == "success"
        ),
        None,
    )
    if not isinstance(seed, dict):
        raise ControlledEvidenceError("source plan has no product success seed input")
    plan = {
        "schema_version": PLAN_SCHEMA,
        "batch_id": batch_id,
        "run_id": source["run_id"],
        "authorization_reference": source["authorization_reference"],
        "environment": source["environment"],
        "check_receipts": source["check_receipts"],
        "scenarios": [
            {
                "kind": kind,
                "case": "success",
                "authorized": True,
                "marketplace_id": seed["marketplace_id"],
                "postal_code": seed["postal_code"],
                "input": dict(seed["input"]),
            }
            for kind in kinds
        ],
    }
    return validate_product_dual_plan(plan)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Reuse only an authorized product input and check receipts from an "
            "existing private shadow plan; discard every precomputed legacy result."
        )
    )
    parser.add_argument("source", type=Path)
    parser.add_argument("--batch-id", required=True)
    parser.add_argument(
        "--kind",
        action="append",
        choices=sorted(PRODUCT_KINDS),
        dest="kinds",
    )
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    project_root = Path(__file__).resolve().parents[1]
    try:
        source = json.loads(args.source.read_text(encoding="utf-8"))
        kinds = args.kinds or sorted(PRODUCT_KINDS)
        plan = convert_plan(source, batch_id=args.batch_id, kinds=kinds)
        _atomic_write_outside_project(
            args.output,
            plan,
            project_root=project_root,
        )
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        ControlledEvidenceError,
    ) as exc:
        message = (
            str(exc)
            if isinstance(exc, ControlledEvidenceError)
            else "product dual plan conversion failed"
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
                "ok": True,
                "output": str(args.output.resolve()),
                "scenario_count": len(plan["scenarios"]),
                "kinds": [scenario["kind"] for scenario in plan["scenarios"]],
                "precomputed_legacy_results_copied": False,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
