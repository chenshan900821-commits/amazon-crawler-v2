from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from amazon_crawler.domain.errors import ConflictError
from amazon_crawler.domain.models import CrawlResult, NormalizedInput
from amazon_crawler.domain.resources import SecretText
from amazon_crawler.infra.evidence import public_response_metadata
from amazon_crawler.infra.sqlite_store import SQLiteStore
from scripts.compile_controlled_evidence import ControlledEvidenceError


LOCAL_CHECKS = {"checkpoint_recovery", "cookie_proxy_redaction"}


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _write_json(path: Path, payload: object) -> str:
    if path.is_symlink():
        raise ControlledEvidenceError("refusing to replace a symlinked check artifact")
    raw = _canonical_bytes(payload) + b"\n"
    path.write_bytes(raw)
    return _sha256_bytes(raw)


def _new_store(root: Path, name: str) -> tuple[SQLiteStore, Path]:
    path = root / name
    store = SQLiteStore(path)
    store.initialize()
    return store, path


def _input(asin: str) -> NormalizedInput:
    return NormalizedInput(
        asin=asin,
        marketplace_id="US",
        source=asin,
        product_url=f"https://www.amazon.com/dp/{asin}",
        postal_code="10001",
    )


def _result(asin: str, evidence: dict[str, Any] | None = None) -> CrawlResult:
    return CrawlResult(
        data={"asin": asin, "title": "Controlled recovery sample"},
        evidence=evidence or {"sha256": hashlib.sha256(asin.encode()).hexdigest()},
        schema_version="controlled.checkpoint.v1",
    )


def run_checkpoint_recovery_check(root: Path) -> dict[str, Any]:
    store, path = _new_store(root, "checkpoint.db")
    job, created = store.create_job(
        kind="product",
        execution_mode="standard",
        priority=0,
        inputs=[_input("B000000001"), _input("B000000002")],
        options={},
        idempotency_key="controlled-checkpoint-recovery",
        max_attempts=5,
    )
    if not created:
        raise ControlledEvidenceError("checkpoint self-test job was not created")
    first = store.claim_next("checkpoint-first", 60)
    if first is None:
        raise ControlledEvidenceError("checkpoint self-test could not claim first item")
    store.complete_item(first, _result(str(first.input["asin"])))
    after_first = store.get_job(job["id"])
    if after_first["checkpoint_seq"] != 1 or after_first["succeeded_items"] != 1:
        raise ControlledEvidenceError("checkpoint did not advance after first result")

    stale = store.claim_next("checkpoint-stale", 0)
    if stale is None:
        raise ControlledEvidenceError("checkpoint self-test could not create an expired lease")
    recovered_count = store.recover_expired_leases()
    if recovered_count != 1:
        raise ControlledEvidenceError("expired lease was not recovered exactly once")
    recovered = store.claim_next("checkpoint-recovered", 60)
    if recovered is None or recovered.id != stale.id:
        raise ControlledEvidenceError("recovered lease did not return the same item")
    stale_rejected = False
    try:
        store.complete_item(stale, _result(str(stale.input["asin"])))
    except ConflictError:
        stale_rejected = True
    if not stale_rejected:
        raise ControlledEvidenceError("stale worker result was not rejected")
    store.complete_item(recovered, _result(str(recovered.input["asin"])))

    reopened = SQLiteStore(path)
    reopened.initialize()
    final_job = reopened.get_job(job["id"])
    results = reopened.list_results(job["id"], limit=10)
    if (
        final_job["status"] != "succeeded"
        or final_job["checkpoint_seq"] != 2
        or len(results) != 2
        or reopened.claim_next("checkpoint-after-restart", 60) is not None
    ):
        raise ControlledEvidenceError("checkpoint state did not survive restart")
    return {
        "schema_version": "controlled-check-artifact.v1",
        "check": "checkpoint_recovery",
        "status": "passed",
        "observations": {
            "first_checkpoint": 1,
            "expired_leases_recovered": recovered_count,
            "stale_result_rejected": stale_rejected,
            "restart_status": final_job["status"],
            "restart_checkpoint": final_job["checkpoint_seq"],
            "result_count": len(results),
        },
    }


def run_cookie_proxy_redaction_check(root: Path) -> dict[str, Any]:
    sentinel = f"controlled-{secrets.token_urlsafe(24)}"
    secret = SecretText(sentinel)
    if sentinel in str(secret) or sentinel in repr(secret):
        raise ControlledEvidenceError("SecretText representation exposed its value")
    metadata = public_response_metadata(
        url=f"https://www.amazon.com/dp/B000000001?token={sentinel}#private",
        status_code=200,
        headers={
            "Content-Type": "text/html",
            "Set-Cookie": f"session={sentinel}",
            "Proxy-Authenticate": sentinel,
            "X-Request-Token": sentinel,
        },
    )
    rendered_metadata = _canonical_bytes(metadata)
    if sentinel.encode() in rendered_metadata:
        raise ControlledEvidenceError("public response metadata exposed the sentinel")

    store, path = _new_store(root, "redaction.db")
    job, _ = store.create_job(
        kind="product",
        execution_mode="standard",
        priority=0,
        inputs=[_input("B000000003")],
        options={},
        idempotency_key="controlled-redaction",
        max_attempts=1,
    )
    item = store.claim_next("redaction-worker", 60)
    if item is None:
        raise ControlledEvidenceError("redaction self-test could not claim its item")
    store.complete_item(item, _result(str(item.input["asin"]), metadata))
    public_payload = {
        "job": store.get_job(job["id"]),
        "results": store.list_results(job["id"], limit=10),
        "events": store.list_events(job["id"], limit=100),
        "secret_text": str(secret),
        "secret_repr": repr(secret),
    }
    rendered_public = _canonical_bytes(public_payload)
    persisted_files = b"".join(
        candidate.read_bytes()
        for candidate in sorted(root.glob(f"{path.name}*"))
        if candidate.is_file()
    )
    if sentinel.encode() in rendered_public or sentinel.encode() in persisted_files:
        raise ControlledEvidenceError("secret sentinel reached durable or public state")
    return {
        "schema_version": "controlled-check-artifact.v1",
        "check": "cookie_proxy_redaction",
        "status": "passed",
        "sentinel_sha256": hashlib.sha256(sentinel.encode()).hexdigest(),
        "observations": {
            "secret_text_redacted": str(secret) == "<redacted>",
            "secret_repr_redacted": repr(secret) == "SecretText(<redacted>)",
            "set_cookie_dropped": "set-cookie" not in metadata["response_headers"],
            "proxy_authenticate_dropped": (
                "proxy-authenticate" not in metadata["response_headers"]
            ),
            "query_and_fragment_removed": (
                metadata["source_url"]
                == "https://www.amazon.com/dp/B000000001"
            ),
            "sqlite_scan_clean": sentinel.encode() not in persisted_files,
            "public_payload_scan_clean": sentinel.encode() not in rendered_public,
        },
    }


def generate_local_check_receipts(
    *,
    run_id: str,
    authorization_reference: str,
    output_dir: Path,
    project_root: Path,
) -> list[Path]:
    if not run_id.strip() or not authorization_reference.strip():
        raise ControlledEvidenceError("run ID and authorization reference are required")
    if output_dir.is_symlink():
        raise ControlledEvidenceError("refusing a symlinked check output directory")
    output_dir = output_dir.resolve()
    if output_dir.is_relative_to(project_root.resolve()):
        raise ControlledEvidenceError(
            "controlled check artifacts must be written outside the project"
        )
    output_dir.mkdir(parents=True, exist_ok=True)
    validated_at = datetime.now(UTC).isoformat()
    written: list[Path] = []
    with tempfile.TemporaryDirectory(prefix="amazon-crawler-local-checks-") as tempdir:
        root = Path(tempdir)
        artifacts = {
            "checkpoint_recovery": run_checkpoint_recovery_check(root),
            "cookie_proxy_redaction": run_cookie_proxy_redaction_check(root),
        }
    for check in sorted(LOCAL_CHECKS):
        artifact_path = output_dir / f"{check}-artifact.json"
        artifact_sha = _write_json(artifact_path, artifacts[check])
        receipt = {
            "schema_version": "controlled-check-receipt.v1",
            "check": check,
            "status": "passed",
            "environment": "isolated",
            "run_id": run_id,
            "authorization_reference": authorization_reference,
            "validated_at": validated_at,
            "artifact_sha256": artifact_sha,
        }
        receipt_path = output_dir / f"{check}.json"
        _write_json(receipt_path, receipt)
        written.extend((artifact_path, receipt_path))
    return written


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Generate checkpoint-recovery and secret-redaction check receipts. "
            "This command performs no network request and no external write."
        )
    )
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--authorization-reference", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    project_root = Path(__file__).resolve().parents[1]
    try:
        written = generate_local_check_receipts(
            run_id=args.run_id,
            authorization_reference=args.authorization_reference,
            output_dir=args.output_dir,
            project_root=project_root,
        )
    except (OSError, ControlledEvidenceError) as exc:
        message = (
            str(exc)
            if isinstance(exc, ControlledEvidenceError)
            else "controlled local checks could not be written"
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
                "network_requests": 0,
                "external_writes": 0,
                "artifacts": [str(path) for path in written],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
