from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from amazon_crawler.plugins.merchant_parser import parse_merchant_home
from amazon_crawler.plugins.response_policy import classify_collection_completeness


def _html_path(snapshot: Path, payload: dict[str, Any]) -> Path | None:
    sibling = snapshot.with_suffix(".html")
    if sibling.is_file():
        return sibling
    raw = payload.get("html_path")
    if isinstance(raw, str):
        candidate = Path(raw)
        if candidate.is_file():
            return candidate
        candidate = snapshot.parent / candidate.name
        if candidate.is_file():
            return candidate
    return None


def audit(snapshot_root: Path) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    for snapshot in sorted(snapshot_root.glob("*.json")):
        payload = json.loads(snapshot.read_text(encoding="utf-8"))
        html_path = _html_path(snapshot, payload)
        if payload.get("task_name") != "merchant_home" or html_path is None:
            continue
        html = html_path.read_text(encoding="utf-8", errors="replace")
        failure = classify_collection_completeness("merchant_home", html)
        parser_rejected = False
        try:
            parse_merchant_home(html, dict(payload.get("task_info") or {}))
        except ValueError:
            parser_rejected = True
        old_rejected = payload.get("error_message") == "商品数量异常"
        matches = bool(
            old_rejected
            and failure
            and failure.code == "upstream_incomplete"
            and failure.retryable
            and parser_rejected
        )
        checks.append(
            {
                "snapshot": snapshot.name,
                "old_rejected_incomplete_page": old_rejected,
                "v2_failure_code": failure.code if failure else None,
                "v2_retryable": failure.retryable if failure else None,
                "parser_rejected": parser_rejected,
                "matches": matches,
            }
        )
    return {
        "ok": bool(checks) and all(check["matches"] for check in checks),
        "snapshot_count": len(checks),
        "checks": checks,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--snapshot-root",
        type=Path,
        default=(
            Path(__file__).resolve().parents[2]
            / "debug_html"
            / "merchant_home"
        ),
    )
    args = parser.parse_args()
    report = audit(args.snapshot_root.resolve())
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
