from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from scripts.verify_parity_manifest import EXPECTED_TASKS, SHA256_PATTERN
from scripts.runtime_source_fingerprint import runtime_source_sha256


SCENARIO_CASES = {"success", "no_result", "throttle"}
REQUIRED_GLOBAL_CHECKS = {
    "cookie_proxy_redaction",
    "checkpoint_recovery",
    "legacy_bridge_roundtrip",
    "tls_impersonation",
}
SENSITIVE_KEYS = {
    "amazon_cookie",
    "authorization",
    "cookie",
    "cookie_header",
    "database_url",
    "http_proxy",
    "legacy_mysql_url",
    "legacy_result_redis_url",
    "merchant_cookie",
    "password",
    "proxy_password",
    "proxy_url",
    "redis_url",
    "secret",
    "token",
}
CREDENTIAL_URI = re.compile(
    r"(?:redis|rediss|mysql|mariadb|https?)://[^\s/'\"]+:[^\s@/'\"]+@",
    re.IGNORECASE,
)


class ControlledEvidenceError(ValueError):
    pass


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and SHA256_PATTERN.fullmatch(value) is not None


def _validate_timestamp(value: object) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ControlledEvidenceError("validated_at is required")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ControlledEvidenceError("validated_at must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ControlledEvidenceError("validated_at must include a timezone")


def _scan_for_secret_material(value: object, *, path: str = "report") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            rendered_key = str(key).strip().lower()
            if rendered_key in SENSITIVE_KEYS:
                raise ControlledEvidenceError(
                    f"controlled report contains forbidden secret field at {path}.{key}"
                )
            _scan_for_secret_material(child, path=f"{path}.{key}")
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            _scan_for_secret_material(child, path=f"{path}[{index}]")
        return
    if isinstance(value, str) and CREDENTIAL_URI.search(value):
        raise ControlledEvidenceError(
            f"controlled report contains a credential-bearing URI at {path}"
        )


def validate_run_report(report: object) -> dict[str, Any]:
    if not isinstance(report, dict):
        raise ControlledEvidenceError("controlled run report must be an object")
    _scan_for_secret_material(report)
    if report.get("schema_version") != "controlled-run-report.v1":
        raise ControlledEvidenceError(
            "schema_version must be controlled-run-report.v1"
        )
    if report.get("environment") != "isolated":
        raise ControlledEvidenceError("environment must be isolated")
    for field in ("run_id", "authorization_reference"):
        value = report.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ControlledEvidenceError(f"{field} is required")
    _validate_timestamp(report.get("validated_at"))

    runtime = report.get("runtime")
    if not isinstance(runtime, dict):
        raise ControlledEvidenceError("runtime must be an object")
    if runtime.get("transport_backend") != "curl_cffi":
        raise ControlledEvidenceError("runtime must use curl_cffi")
    if runtime.get("tls_impersonation") is not True:
        raise ControlledEvidenceError("runtime must enable TLS impersonation")
    source_hash = runtime.get("v2_source_sha256")
    current_source_hash = runtime_source_sha256(Path(__file__).resolve().parents[1])
    if not _is_sha256(source_hash) or source_hash != current_source_hash:
        raise ControlledEvidenceError(
            "runtime V2 source fingerprint is missing or stale"
        )

    checks = report.get("checks")
    if not isinstance(checks, dict):
        raise ControlledEvidenceError("checks must be an object")
    missing_checks = sorted(
        check for check in REQUIRED_GLOBAL_CHECKS if checks.get(check) is not True
    )
    if missing_checks:
        raise ControlledEvidenceError(
            f"controlled run has failing checks: {missing_checks}"
        )

    check_receipts = report.get("check_receipts")
    if (
        not isinstance(check_receipts, dict)
        or set(check_receipts) != REQUIRED_GLOBAL_CHECKS
    ):
        raise ControlledEvidenceError(
            "controlled run requires a receipt for every global check"
        )
    for check in sorted(REQUIRED_GLOBAL_CHECKS):
        receipt = check_receipts[check]
        if not isinstance(receipt, dict):
            raise ControlledEvidenceError(f"check receipt {check} must be an object")
        if (
            receipt.get("schema_version") != "controlled-check-receipt.v1"
            or receipt.get("check") != check
            or receipt.get("status") != "passed"
            or receipt.get("environment") != "isolated"
            or receipt.get("run_id") != report["run_id"]
            or receipt.get("authorization_reference")
            != report["authorization_reference"]
            or not _is_sha256(receipt.get("artifact_sha256"))
        ):
            raise ControlledEvidenceError(f"check receipt {check} is invalid")
        _validate_timestamp(receipt.get("validated_at"))

    scenarios = report.get("scenarios")
    if not isinstance(scenarios, list):
        raise ControlledEvidenceError("scenarios must be a list")
    expected_count = len(EXPECTED_TASKS) * len(SCENARIO_CASES)
    if len(scenarios) != expected_count:
        raise ControlledEvidenceError(
            f"controlled run requires exactly {expected_count} scenarios"
        )

    identities: set[tuple[str, str]] = set()
    marketplaces: set[str] = set()
    postal_hashes: set[str] = set()
    for index, scenario in enumerate(scenarios):
        if not isinstance(scenario, dict):
            raise ControlledEvidenceError(f"scenario {index} must be an object")
        kind = scenario.get("kind")
        case = scenario.get("case")
        if kind not in EXPECTED_TASKS:
            raise ControlledEvidenceError(f"scenario {index} has unsupported kind")
        if case not in SCENARIO_CASES:
            raise ControlledEvidenceError(f"scenario {index} has unsupported case")
        identity = (str(kind), str(case))
        if identity in identities:
            raise ControlledEvidenceError(f"duplicate scenario {kind}/{case}")
        identities.add(identity)
        if scenario.get("status") != "passed":
            raise ControlledEvidenceError(f"scenario {kind}/{case} did not pass")
        if scenario.get("authorized") is not True:
            raise ControlledEvidenceError(f"scenario {kind}/{case} is not authorized")
        if scenario.get("field_contract_match") is not True:
            raise ControlledEvidenceError(
                f"scenario {kind}/{case} field contract did not match"
            )
        if scenario.get("outcome_match") is not True:
            raise ControlledEvidenceError(
                f"scenario {kind}/{case} outcome did not match"
            )
        if scenario.get("differences") != []:
            raise ControlledEvidenceError(
                f"scenario {kind}/{case} contains unresolved differences"
            )
        for field in (
            "sample_hash",
            "input_hash",
            "response_sha256",
            "legacy_result_hash",
            "v2_result_hash",
            "postal_code_hash",
        ):
            if not _is_sha256(scenario.get(field)):
                raise ControlledEvidenceError(
                    f"scenario {kind}/{case} requires SHA-256 field {field}"
                )
        marketplace = scenario.get("marketplace_id")
        if not isinstance(marketplace, str) or not marketplace.strip():
            raise ControlledEvidenceError(
                f"scenario {kind}/{case} requires marketplace_id"
            )
        marketplaces.add(marketplace.upper())
        postal_hashes.add(str(scenario["postal_code_hash"]))

    expected_identities = {
        (kind, case) for kind in EXPECTED_TASKS for case in SCENARIO_CASES
    }
    if identities != expected_identities:
        raise ControlledEvidenceError("scenario matrix is incomplete")
    if not {"US", "JP"}.issubset(marketplaces):
        raise ControlledEvidenceError("controlled run must cover US and JP")
    if len(postal_hashes) < 2:
        raise ControlledEvidenceError(
            "controlled run must cover at least two distinct postal codes"
        )
    return report


def validate_compiled_pair(
    receipt: object,
    comparison: object,
    *,
    expected_kind: str,
) -> None:
    if not isinstance(receipt, dict) or not isinstance(comparison, dict):
        raise ControlledEvidenceError("compiled evidence artifacts must be objects")
    _scan_for_secret_material(receipt, path="receipt")
    _scan_for_secret_material(comparison, path="comparison")
    if receipt.get("schema_version") != "controlled-integration-evidence.v1":
        raise ControlledEvidenceError("compiled receipt schema is invalid")
    if comparison.get("schema_version") != "controlled-comparison-report.v1":
        raise ControlledEvidenceError("compiled comparison schema is invalid")
    if receipt.get("kind") != expected_kind or comparison.get("kind") != expected_kind:
        raise ControlledEvidenceError("compiled evidence kind does not match")
    for field in ("run_id", "validated_at", "authorization_reference"):
        if receipt.get(field) != comparison.get(field):
            raise ControlledEvidenceError(
                f"compiled evidence field {field} does not match"
            )
    if receipt.get("runtime") != comparison.get("runtime"):
        raise ControlledEvidenceError("compiled evidence runtime does not match")
    if receipt.get("check_receipts") != comparison.get("check_receipts"):
        raise ControlledEvidenceError(
            "compiled evidence check receipts do not match"
        )
    scenarios = comparison.get("scenarios")
    if not isinstance(scenarios, list) or len(scenarios) != len(SCENARIO_CASES):
        raise ControlledEvidenceError("compiled comparison requires three scenarios")
    cases = {scenario.get("case") for scenario in scenarios if isinstance(scenario, dict)}
    if cases != SCENARIO_CASES:
        raise ControlledEvidenceError("compiled comparison scenario cases are incomplete")
    for scenario in scenarios:
        if (
            not isinstance(scenario, dict)
            or scenario.get("kind") != expected_kind
            or scenario.get("status") != "passed"
            or scenario.get("authorized") is not True
            or scenario.get("field_contract_match") is not True
            or scenario.get("outcome_match") is not True
            or scenario.get("differences") != []
            or not _is_sha256(scenario.get("sample_hash"))
        ):
            raise ControlledEvidenceError(
                "compiled comparison contains a failing scenario"
            )
    expected_hashes = [scenario["sample_hash"] for scenario in scenarios]
    if receipt.get("sample_hashes") != expected_hashes:
        raise ControlledEvidenceError(
            "compiled receipt sample hashes do not match its comparison"
        )


def compile_evidence(
    report: dict[str, Any],
    *,
    project_root: Path,
    output_dir: Path,
) -> list[Path]:
    report = validate_run_report(report)
    project_root = project_root.resolve()
    output_dir = output_dir.resolve()
    if not output_dir.is_relative_to(project_root):
        raise ControlledEvidenceError("output directory must be inside the project")
    output_dir.mkdir(parents=True, exist_ok=True)

    def write_artifact(path: Path, payload: object) -> None:
        if path.is_symlink():
            raise ControlledEvidenceError(
                "refusing to replace a symlinked evidence artifact"
            )
        path.write_bytes(_canonical_bytes(payload) + b"\n")

    written: list[Path] = []
    scenarios = list(report["scenarios"])
    for kind in sorted(EXPECTED_TASKS):
        kind_scenarios = sorted(
            (scenario for scenario in scenarios if scenario["kind"] == kind),
            key=lambda scenario: scenario["case"],
        )
        comparison = {
            "schema_version": "controlled-comparison-report.v1",
            "kind": kind,
            "run_id": report["run_id"],
            "validated_at": report["validated_at"],
            "authorization_reference": report["authorization_reference"],
            "environment": "isolated",
            "runtime": report["runtime"],
            "check_receipts": report["check_receipts"],
            "scenarios": kind_scenarios,
        }
        comparison_path = output_dir / f"{kind}-comparison.json"
        write_artifact(comparison_path, comparison)
        comparison_sha = hashlib.sha256(comparison_path.read_bytes()).hexdigest()
        comparison_relative = str(comparison_path.relative_to(project_root))

        receipt = {
            "schema_version": "controlled-integration-evidence.v1",
            "kind": kind,
            "status": "passed",
            "environment": "isolated",
            "run_id": report["run_id"],
            "validated_at": report["validated_at"],
            "authorization_reference": report["authorization_reference"],
            "sample_hashes": [
                scenario["sample_hash"] for scenario in kind_scenarios
            ],
            "checks": {
                "authorized_samples": True,
                "legacy_shadow_match": True,
                "cookie_proxy_redaction": True,
                "checkpoint_recovery": True,
                "legacy_bridge_roundtrip": True,
                "tls_impersonation": True,
            },
            "runtime": report["runtime"],
            "check_receipts": report["check_receipts"],
            "comparison_report": {
                "path": comparison_relative,
                "sha256": comparison_sha,
            },
        }
        receipt_path = output_dir / f"{kind}.json"
        write_artifact(receipt_path, receipt)
        written.extend((comparison_path, receipt_path))
    return written


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Compile strict, redacted controlled-integration receipts from an "
            "already executed isolated run report. This command performs no "
            "network requests and does not update the parity manifest."
        )
    )
    parser.add_argument("report", type=Path)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("evidence/controlled"),
    )
    args = parser.parse_args()
    project_root = Path(__file__).resolve().parents[1]
    try:
        report = json.loads(args.report.read_text(encoding="utf-8"))
        written = compile_evidence(
            report,
            project_root=project_root,
            output_dir=(
                args.output_dir
                if args.output_dir.is_absolute()
                else project_root / args.output_dir
            ),
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ControlledEvidenceError) as exc:
        message = (
            str(exc)
            if isinstance(exc, ControlledEvidenceError)
            else "controlled report could not be read or written"
        )
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": {
                        "type": type(exc).__name__,
                        "message": message,
                    },
                },
                ensure_ascii=False,
            )
        )
        return 2
    print(
        json.dumps(
            {
                "ok": True,
                "artifacts": [
                    str(path.relative_to(project_root)) for path in written
                ],
                "manifest_updated": False,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
