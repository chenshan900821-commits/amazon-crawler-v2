from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path

from scripts.runtime_source_fingerprint import runtime_source_sha256


EXPECTED_TASKS = {
    "search",
    "search_hour",
    "product",
    "product_hw",
    "product_time",
    "reviews",
    "merchant",
    "merchant_home",
    "merchant_products",
    "category_asin_list",
    "rank_list",
}
EXPECTED_SHARED_CAPABILITIES = {
    "agent_skill_boundary",
    "api_management_ui",
    "cookie_production_scheduler",
    "cookie_provider_routing",
    "durable_control_plane",
    "legacy_mysql_redis_bridge",
    "multi_result_storage",
    "proxy_tls_fingerprint",
    "secret_boundary",
}
REQUIRED_SHARED_EVIDENCE = {"implementation", "test"}
CONTROLLED_COOKIE_EVIDENCE_TYPE = "controlled_cookie_production"
COOKIE_PRODUCTION_CHECKS = {
    "external_write_authorized",
    "address_confirmed",
    "ttl_verified",
    "consumer_visible",
    "cookie_values_redacted",
    "maintenance_run_once_passed",
}
REQUIRED_RESOURCES = {"cookie", "proxy", "fingerprint"}
COMPLETE_EVIDENCE = {
    "contract_test",
    "offline_replay",
    "checkpoint_recovery_test",
    "migration_mapping_test",
    "controlled_integration",
}
CONTROLLED_CHECKS = {
    "authorized_samples",
    "legacy_shadow_match",
    "cookie_proxy_redaction",
    "checkpoint_recovery",
    "legacy_bridge_roundtrip",
    "tls_impersonation",
}
CONTROLLED_RECEIPT_CHECKS = {
    "cookie_proxy_redaction",
    "checkpoint_recovery",
    "legacy_bridge_roundtrip",
    "tls_impersonation",
}
CONTROLLED_SCENARIO_CASES = {"success", "no_result", "throttle"}
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def _validate_cookie_production_evidence(evidence_path: Path) -> list[str]:
    try:
        receipt = json.loads(evidence_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return ["cookie production controlled evidence is not valid JSON"]
    if not isinstance(receipt, dict):
        return ["cookie production controlled evidence must be an object"]
    errors: list[str] = []
    expected = {
        "schema_version": "controlled-cookie-production-evidence.v1",
        "capability": "cookie_production_scheduler",
        "status": "passed",
        "environment": "isolated",
    }
    for field, value in expected.items():
        if receipt.get(field) != value:
            errors.append(f"cookie production evidence {field} must be {value!r}")
    for field in ("validated_at", "authorization_reference"):
        value = receipt.get(field)
        if not isinstance(value, str) or not value.strip():
            errors.append(f"cookie production evidence requires {field}")
    validated_at = receipt.get("validated_at")
    if isinstance(validated_at, str) and validated_at.strip():
        try:
            parsed = datetime.fromisoformat(validated_at.replace("Z", "+00:00"))
        except ValueError:
            errors.append("cookie production evidence validated_at must be ISO-8601")
        else:
            if parsed.tzinfo is None:
                errors.append(
                    "cookie production evidence validated_at must include a timezone"
                )

    runtime = receipt.get("runtime")
    current_hash = runtime_source_sha256(Path(__file__).resolve().parents[1])
    if (
        not isinstance(runtime, dict)
        or set(runtime) != {
            "transport_backend",
            "tls_impersonation",
            "v2_source_sha256",
        }
        or runtime.get("transport_backend") != "curl_cffi"
        or runtime.get("tls_impersonation") is not True
        or runtime.get("v2_source_sha256") != current_hash
    ):
        errors.append(
            "cookie production evidence requires current curl_cffi TLS runtime"
        )

    checks = receipt.get("checks")
    if not isinstance(checks, dict) or set(checks) != COOKIE_PRODUCTION_CHECKS:
        errors.append("cookie production evidence requires every controlled check")
    elif any(checks.get(name) is not True for name in COOKIE_PRODUCTION_CHECKS):
        errors.append("cookie production evidence contains a failing controlled check")

    operations = receipt.get("operations")
    marketplaces: set[str] = set()
    postal_hashes: set[str] = set()
    if not isinstance(operations, list) or len(operations) < 2:
        errors.append("cookie production evidence requires at least two operations")
    else:
        required_fields = {
            "pool",
            "marketplace_id",
            "postal_code_hash",
            "available_before",
            "created",
            "rejected",
            "available_after",
            "ttl_seconds",
            "address_confirmed",
            "consumer_visible",
        }
        for index, operation in enumerate(operations):
            if not isinstance(operation, dict) or set(operation) != required_fields:
                errors.append(f"cookie production operation {index} has invalid fields")
                continue
            marketplace = operation.get("marketplace_id")
            postal_hash = operation.get("postal_code_hash")
            available_before = operation.get("available_before")
            created = operation.get("created")
            rejected = operation.get("rejected")
            available_after = operation.get("available_after")
            ttl_seconds = operation.get("ttl_seconds")
            if operation.get("pool") not in {"default", "overseas"}:
                errors.append(f"cookie production operation {index} has invalid pool")
            if marketplace not in {"US", "JP"}:
                errors.append(
                    f"cookie production operation {index} has invalid marketplace"
                )
            else:
                marketplaces.add(str(marketplace))
            if not isinstance(postal_hash, str) or not SHA256_PATTERN.fullmatch(
                postal_hash
            ):
                errors.append(
                    f"cookie production operation {index} has invalid postal hash"
                )
            else:
                postal_hashes.add(postal_hash)
            counts = (available_before, created, rejected, available_after)
            if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in counts):
                errors.append(f"cookie production operation {index} has invalid counts")
            elif created < 1 or available_after < available_before + created:
                errors.append(
                    f"cookie production operation {index} did not prove a new visible cookie"
                )
            if (
                isinstance(ttl_seconds, bool)
                or not isinstance(ttl_seconds, int)
                or ttl_seconds < 60
            ):
                errors.append(f"cookie production operation {index} has invalid TTL")
            if operation.get("address_confirmed") is not True:
                errors.append(
                    f"cookie production operation {index} did not confirm address"
                )
            if operation.get("consumer_visible") is not True:
                errors.append(
                    f"cookie production operation {index} was not consumer-visible"
                )
        if not {"US", "JP"}.issubset(marketplaces):
            errors.append("cookie production evidence must cover US and JP")
        if len(postal_hashes) < 2:
            errors.append(
                "cookie production evidence must cover at least two postal codes"
            )
    return errors


def _validate_controlled_evidence(
    *, project_root: Path, kind: str, evidence_path: Path
) -> list[str]:
    try:
        receipt = json.loads(evidence_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return [f"{kind}: controlled integration evidence is not valid JSON"]
    if not isinstance(receipt, dict):
        return [f"{kind}: controlled integration evidence must be an object"]

    errors: list[str] = []
    expected = {
        "schema_version": "controlled-integration-evidence.v1",
        "kind": kind,
        "status": "passed",
        "environment": "isolated",
    }
    for field, value in expected.items():
        if receipt.get(field) != value:
            errors.append(f"{kind}: controlled evidence {field} must be {value!r}")
    for field in ("validated_at", "authorization_reference"):
        if not isinstance(receipt.get(field), str) or not receipt[field].strip():
            errors.append(f"{kind}: controlled evidence requires {field}")

    sample_hashes = receipt.get("sample_hashes")
    if (
        not isinstance(sample_hashes, list)
        or len(sample_hashes) < 3
        or not all(isinstance(value, str) and SHA256_PATTERN.fullmatch(value) for value in sample_hashes)
    ):
        errors.append(f"{kind}: controlled evidence requires at least 3 SHA-256 sample hashes")

    checks = receipt.get("checks")
    if not isinstance(checks, dict):
        errors.append(f"{kind}: controlled evidence checks must be an object")
    else:
        missing = sorted(name for name in CONTROLLED_CHECKS if checks.get(name) is not True)
        if missing:
            errors.append(f"{kind}: controlled evidence has failing checks {missing}")

    runtime = receipt.get("runtime")
    if not isinstance(runtime, dict) or runtime.get("transport_backend") != "curl_cffi" or runtime.get("tls_impersonation") is not True:
        errors.append(f"{kind}: controlled evidence requires curl_cffi TLS impersonation")
    elif (
        not isinstance(runtime.get("v2_source_sha256"), str)
        or not SHA256_PATTERN.fullmatch(runtime["v2_source_sha256"])
        or runtime["v2_source_sha256"]
        != runtime_source_sha256(Path(__file__).resolve().parents[1])
    ):
        errors.append(f"{kind}: controlled evidence V2 source fingerprint is stale")

    check_receipts = receipt.get("check_receipts")
    if (
        not isinstance(check_receipts, dict)
        or set(check_receipts) != CONTROLLED_RECEIPT_CHECKS
    ):
        errors.append(f"{kind}: controlled evidence requires all check receipts")
    else:
        for check in sorted(CONTROLLED_RECEIPT_CHECKS):
            check_receipt = check_receipts[check]
            if (
                not isinstance(check_receipt, dict)
                or check_receipt.get("schema_version")
                != "controlled-check-receipt.v1"
                or check_receipt.get("check") != check
                or check_receipt.get("status") != "passed"
                or check_receipt.get("environment") != "isolated"
                or check_receipt.get("run_id") != receipt.get("run_id")
                or check_receipt.get("authorization_reference")
                != receipt.get("authorization_reference")
                or not isinstance(check_receipt.get("artifact_sha256"), str)
                or not SHA256_PATTERN.fullmatch(check_receipt["artifact_sha256"])
                or not isinstance(check_receipt.get("validated_at"), str)
                or not check_receipt["validated_at"].strip()
            ):
                errors.append(f"{kind}: controlled check receipt {check} is invalid")

    report = receipt.get("comparison_report")
    if not isinstance(report, dict):
        errors.append(f"{kind}: controlled evidence requires comparison_report")
    else:
        raw_report_path = report.get("path")
        expected_sha = report.get("sha256")
        if not isinstance(raw_report_path, str) or not raw_report_path.strip():
            errors.append(f"{kind}: comparison report path is missing")
        elif not isinstance(expected_sha, str) or not SHA256_PATTERN.fullmatch(expected_sha):
            errors.append(f"{kind}: comparison report SHA-256 is invalid")
        else:
            unresolved_report_path = project_root / raw_report_path
            report_path = unresolved_report_path.resolve()
            if unresolved_report_path.is_symlink():
                errors.append(f"{kind}: comparison report must not be a symlink")
            elif not report_path.is_relative_to(project_root) or not report_path.is_file():
                errors.append(f"{kind}: comparison report does not exist: {raw_report_path}")
            elif hashlib.sha256(report_path.read_bytes()).hexdigest() != expected_sha:
                errors.append(f"{kind}: comparison report SHA-256 does not match")
            else:
                try:
                    comparison = json.loads(report_path.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                    errors.append(f"{kind}: comparison report is not valid JSON")
                else:
                    if not isinstance(comparison, dict):
                        errors.append(f"{kind}: comparison report must be an object")
                    else:
                        expected_comparison = {
                            "schema_version": "controlled-comparison-report.v1",
                            "kind": kind,
                            "run_id": receipt.get("run_id"),
                            "validated_at": receipt.get("validated_at"),
                            "authorization_reference": receipt.get(
                                "authorization_reference"
                            ),
                            "environment": "isolated",
                            "runtime": runtime,
                            "check_receipts": check_receipts,
                        }
                        for field, value in expected_comparison.items():
                            if comparison.get(field) != value:
                                errors.append(
                                    f"{kind}: comparison report {field} does not match"
                                )
                        scenarios = comparison.get("scenarios")
                        if not isinstance(scenarios, list) or len(scenarios) != 3:
                            errors.append(
                                f"{kind}: comparison report requires three scenarios"
                            )
                        else:
                            cases = {
                                scenario.get("case")
                                for scenario in scenarios
                                if isinstance(scenario, dict)
                            }
                            if cases != CONTROLLED_SCENARIO_CASES:
                                errors.append(
                                    f"{kind}: comparison scenario cases are incomplete"
                                )
                            valid_scenarios = all(
                                isinstance(scenario, dict)
                                and scenario.get("kind") == kind
                                and scenario.get("status") == "passed"
                                and scenario.get("authorized") is True
                                and scenario.get("field_contract_match") is True
                                and scenario.get("outcome_match") is True
                                and scenario.get("differences") == []
                                and isinstance(scenario.get("sample_hash"), str)
                                and SHA256_PATTERN.fullmatch(
                                    scenario["sample_hash"]
                                )
                                for scenario in scenarios
                            )
                            if not valid_scenarios:
                                errors.append(
                                    f"{kind}: comparison report contains a failing scenario"
                                )
                            elif sample_hashes != [
                                scenario["sample_hash"] for scenario in scenarios
                            ]:
                                errors.append(
                                    f"{kind}: controlled sample hashes do not match comparison"
                                )
    return errors


def validate_manifest(path: Path, *, require_complete: bool) -> list[str]:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    project_root = path.resolve().parent.parent
    errors: list[str] = []
    shared = manifest.get("shared_capabilities")
    if not isinstance(shared, list):
        errors.append("shared_capabilities must be a list")
        shared = []
    shared_names = [item.get("name") for item in shared if isinstance(item, dict)]
    missing_shared = EXPECTED_SHARED_CAPABILITIES - set(shared_names)
    unexpected_shared = set(shared_names) - EXPECTED_SHARED_CAPABILITIES
    duplicate_shared = sorted(
        name for name in set(shared_names) if shared_names.count(name) > 1
    )
    if missing_shared:
        errors.append(f"missing shared capabilities: {sorted(missing_shared)}")
    if unexpected_shared:
        errors.append(f"unexpected shared capabilities: {sorted(unexpected_shared)}")
    if duplicate_shared:
        errors.append(f"duplicate shared capabilities: {duplicate_shared}")
    for capability in shared:
        if not isinstance(capability, dict):
            errors.append("shared capability entries must be objects")
            continue
        name = str(capability.get("name", "<unknown>"))
        if capability.get("current_status") != "verified":
            errors.append(f"shared capability {name}: status must be 'verified'")
        evidence = capability.get("evidence")
        if not isinstance(evidence, list):
            errors.append(f"shared capability {name}: evidence must be a list")
            continue
        evidence_types: set[object] = set()
        for item in evidence:
            if not isinstance(item, dict) or item.get("passing") is not True:
                errors.append(
                    f"shared capability {name}: every evidence item must be passing"
                )
                continue
            evidence_types.add(item.get("type"))
            raw_path = item.get("path")
            if not isinstance(raw_path, str) or not raw_path.strip():
                errors.append(
                    f"shared capability {name}: passing evidence requires a path"
                )
                continue
            evidence_path = (project_root / raw_path).resolve()
            if not evidence_path.is_relative_to(project_root) or not evidence_path.is_file():
                errors.append(
                    f"shared capability {name}: evidence path does not exist: {raw_path}"
                )
            elif item.get("type") == CONTROLLED_COOKIE_EVIDENCE_TYPE:
                errors.extend(_validate_cookie_production_evidence(evidence_path))
        missing_types = REQUIRED_SHARED_EVIDENCE - evidence_types
        if missing_types:
            errors.append(
                f"shared capability {name}: missing passing evidence {sorted(missing_types)}"
            )
        if (
            require_complete
            and name == "cookie_production_scheduler"
            and CONTROLLED_COOKIE_EVIDENCE_TYPE not in evidence_types
        ):
            errors.append(
                "shared capability cookie_production_scheduler: missing passing "
                "controlled cookie production evidence"
            )

    tasks = manifest.get("tasks")
    if not isinstance(tasks, list):
        return ["tasks must be a list"]

    kinds = [task.get("kind") for task in tasks]
    missing = EXPECTED_TASKS - set(kinds)
    unexpected = set(kinds) - EXPECTED_TASKS
    duplicates = sorted(kind for kind in set(kinds) if kinds.count(kind) > 1)
    if missing:
        errors.append(f"missing task kinds: {sorted(missing)}")
    if unexpected:
        errors.append(f"unexpected task kinds: {sorted(unexpected)}")
    if duplicates:
        errors.append(f"duplicate task kinds: {duplicates}")

    for task in tasks:
        kind = task.get("kind", "<unknown>")
        for field in (
            "legacy_task_name",
            "legacy_task_table",
            "legacy_result_table",
            "inputs",
            "pagination",
            "resources",
            "current_status",
            "evidence",
        ):
            if field not in task:
                errors.append(f"{kind}: missing {field}")
        if not task.get("results") and not task.get("results_contract"):
            errors.append(f"{kind}: missing result contract")
        resources = set(task.get("resources", []))
        if not REQUIRED_RESOURCES.issubset(resources):
            errors.append(f"{kind}: missing resources {sorted(REQUIRED_RESOURCES - resources)}")
        for item in task.get("evidence", []):
            if not isinstance(item, dict) or item.get("passing") is not True:
                continue
            raw_path = item.get("path")
            if not isinstance(raw_path, str) or not raw_path.strip():
                errors.append(f"{kind}: passing evidence requires a path")
                continue
            evidence_path = (project_root / raw_path).resolve()
            if not evidence_path.is_relative_to(project_root) or not evidence_path.is_file():
                errors.append(f"{kind}: evidence path does not exist: {raw_path}")
            elif item.get("type") == "controlled_integration":
                errors.extend(
                    _validate_controlled_evidence(
                        project_root=project_root,
                        kind=str(kind),
                        evidence_path=evidence_path,
                    )
                )

        if require_complete:
            if task.get("current_status") != "complete":
                errors.append(f"{kind}: status is not complete")
            evidence_types = {
                item.get("type")
                for item in task.get("evidence", [])
                if isinstance(item, dict) and item.get("passing") is True
            }
            missing_evidence = COMPLETE_EVIDENCE - evidence_types
            if missing_evidence:
                errors.append(f"{kind}: missing passing evidence {sorted(missing_evidence)}")

    if require_complete and manifest.get("overall_status") != "complete":
        errors.append("overall_status is not complete")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--require-complete", action="store_true")
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "contracts" / "legacy_parity.v1.json",
    )
    args = parser.parse_args()
    errors = validate_manifest(args.manifest, require_complete=args.require_complete)
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print(f"OK: {len(EXPECTED_TASKS)} task contracts are structurally valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
