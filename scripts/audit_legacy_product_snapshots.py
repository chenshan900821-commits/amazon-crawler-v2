from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from amazon_crawler.plugins.amazon_parser import (
    LEGACY_PRODUCT_FIELDS,
    parse_product_html,
)
from amazon_crawler.plugins.marketplaces import AMAZON_ID_TO_MARKETPLACE


DYNAMIC_FIELDS = {"created_at", "generate_date", "task_id", "link"}
SOURCE_EVOLUTION_ALLOWLIST = {
    (
        "product/20260702-135154-ATVPDKIKX0DER-B0GCHH166Z.json",
        "imgs",
    ): {
        "legacy_commit": "5448fd936e1dd619b8963cbb5ca1c8e155d56dfe",
        "reason": (
            "snapshot predates the legacy same-day fix that added large/main "
            "image fallback when hiRes is absent"
        ),
    }
}


def _normalized(value: Any) -> Any:
    if isinstance(value, str):
        return re.sub(r"\s+", " ", value).strip()
    if isinstance(value, list):
        return [_normalized(item) for item in value]
    if isinstance(value, dict):
        return {
            str(key): _normalized(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    return value


def _html_path(snapshot_path: Path, payload: dict[str, Any]) -> Path | None:
    sibling = snapshot_path.with_suffix(".html")
    if sibling.is_file():
        return sibling
    raw = payload.get("html_path")
    if isinstance(raw, str):
        candidate = Path(raw)
        if candidate.is_file():
            return candidate
        by_name = snapshot_path.parent / candidate.name
        if by_name.is_file():
            return by_name
    return None


def audit(snapshot_root: Path, *, include_values: bool = False) -> dict[str, Any]:
    snapshots: list[dict[str, Any]] = []
    for snapshot_path in sorted(snapshot_root.rglob("*.json")):
        try:
            payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        old = payload.get("response_data") if isinstance(payload, dict) else None
        if not isinstance(old, dict) or not old.get("asin"):
            continue
        html_path = _html_path(snapshot_path, payload)
        if html_path is None:
            continue
        market = AMAZON_ID_TO_MARKETPLACE.get(str(old.get("marketplace_id")))
        if market is None:
            continue
        html = html_path.read_text(encoding="utf-8", errors="replace")
        new = parse_product_html(
            html,
            asin=str(old["asin"]),
            product_url=f"https://{market.domain}/dp/{old['asin']}",
            marketplace_id=market.id,
            postal_code=str(old.get("zipcode") or "") or None,
            task_id="snapshot-audit",
        )
        mismatches = [
            field
            for field in LEGACY_PRODUCT_FIELDS
            if field not in DYNAMIC_FIELDS
            and _normalized(old.get(field)) != _normalized(new.get(field))
        ]
        old_dimensions = payload.get("skus_items")
        old_asins = {
            str(row.get("asin"))
            for row in old_dimensions or []
            if isinstance(row, dict) and row.get("asin")
        }
        new_asins = {
            str(row.get("asin"))
            for row in new.get("dimension_items", [])
            if isinstance(row, dict) and row.get("asin")
        }
        relative_snapshot = str(snapshot_path.relative_to(snapshot_root))
        accepted_deltas = {
            field: SOURCE_EVOLUTION_ALLOWLIST[(relative_snapshot, field)]
            for field in mismatches
            if (relative_snapshot, field) in SOURCE_EVOLUTION_ALLOWLIST
        }
        unapproved_mismatches = [
            field for field in mismatches if field not in accepted_deltas
        ]
        item = {
                "snapshot": relative_snapshot,
                "field_mismatches": sorted(mismatches),
                "unapproved_field_mismatches": sorted(unapproved_mismatches),
                "accepted_source_deltas": accepted_deltas,
                "old_dimension_count": len(old_asins),
                "new_dimension_count": len(new_asins),
                "dimension_asins_match": old_asins == new_asins,
            }
        if include_values and mismatches:
            item["field_values"] = {
                field: {
                    "old": _normalized(old.get(field)),
                    "new": _normalized(new.get(field)),
                }
                for field in mismatches
            }
        snapshots.append(item)
    return {
        "ok": bool(snapshots)
        and all(
            not item["unapproved_field_mismatches"]
            and item["dimension_asins_match"]
            for item in snapshots
        ),
        "snapshot_count": len(snapshots),
        "snapshots": snapshots,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--snapshot-root",
        type=Path,
        default=Path(__file__).resolve().parents[2] / "debug_html",
    )
    parser.add_argument(
        "--include-values",
        action="store_true",
        help="include old/new values for mismatched fields",
    )
    args = parser.parse_args()
    report = audit(args.snapshot_root.resolve(), include_values=args.include_values)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
