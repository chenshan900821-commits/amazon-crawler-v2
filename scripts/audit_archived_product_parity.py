#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
import hashlib
import importlib
import io
import json
import sys
import types
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from amazon_crawler.plugins.amazon_parser import parse_product_html
from amazon_crawler.plugins.marketplaces import AMAZON_ID_TO_MARKETPLACE
from scripts.build_controlled_run_report import _differences, _normalize_comparable
from scripts.collect_controlled_v2_shadow import _atomic_write_outside_project
from scripts.compile_controlled_evidence import ControlledEvidenceError


SCHEMA_VERSION = "archived-product-parity-report.v1"
LEGACY_CONFIG_NAMES = {
    "FREQUENT_TEXT",
    "RELOAD_TEXT",
    "CHARACTER_TEXT",
    "CHARACTER_TEXT_MX",
    "CHARACTER_TEXT_BR",
    "CHARACTER_TEXT_ES",
    "CHARACTER_TEXT_AE",
    "CHARACTER_TEXT_DE",
    "CHARACTER_TEXT_FR",
    "CHARACTER_TEXT_IT",
    "CHARACTER_TEXT_NL",
    "CHARACTER_TEXT_PL",
    "CHARACTER_TEXT_SE",
    "CHARACTER_TEXT_BE",
    "CHARACTER_TEXT_TR",
    "CHARACTER_TEXT_OTHER",
    "NO_LIST_PAGE_TEXT",
    "NO_LIST_PAGE_TEXT_BR",
    "NO_LIST_PAGE_TEXT_MX",
    "NO_LIST_TEXT",
    "NO_LIST_TEXT_BR",
    "NO_LIST_TEXT_MX",
    "NO_LIST_TEXT_PL",
    "NO_LIST_TEXT_TR",
    "ADDRESS_ERROR",
    "ADDRESS_ERROR2",
    "ADDRESS_ERROR_TR",
    "ADDRESS_ERROR_PL",
    "ADDRESS_ERROR_FR",
    "ADDRESS_ERROR_IT",
    "ADDRESS_ERROR_SE",
    "ADDRESS_ERROR_ES",
    "UNAVA_UK",
    "MARKET_ID_JSON",
    "SEARCH_LANG",
    "OTHER_TEXT",
}
LegacyParser = Callable[..., tuple[list[dict[str, Any]], dict[str, Any]]]
LegacyCollectionParser = Callable[..., list[dict[str, Any]]]
LegacyMerchantParser = Callable[..., dict[str, Any] | list[dict[str, Any]]]


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class _NullLogger:
    def __getattr__(self, _name: str) -> Callable[..., None]:
        return lambda *_args, **_kwargs: None


def _literal_legacy_config(path: Path) -> dict[str, Any]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, SyntaxError) as exc:
        raise ControlledEvidenceError("legacy parser config could not be read") from exc
    values: dict[str, Any] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if not isinstance(target, ast.Name) or target.id not in LEGACY_CONFIG_NAMES:
                continue
            try:
                values[target.id] = ast.literal_eval(node.value)
            except (ValueError, TypeError) as exc:
                raise ControlledEvidenceError(
                    f"legacy parser config field {target.id} is not a literal"
                ) from exc
    missing = sorted(LEGACY_CONFIG_NAMES - set(values))
    if missing:
        raise ControlledEvidenceError(
            "legacy parser config is missing required parser constants"
        )
    return values


def _load_current_legacy_parser(legacy_source_root: Path) -> LegacyParser:
    legacy_source_root = legacy_source_root.resolve()
    config_values = _literal_legacy_config(legacy_source_root / "settings/config.py")
    try:
        import parsel  # noqa: F401
    except ImportError as exc:
        raise ControlledEvidenceError(
            "current legacy parser replay requires the legacy-parity optional dependencies"
        ) from exc

    settings_module = types.ModuleType("settings")
    settings_module.__path__ = []  # type: ignore[attr-defined]
    config_module = types.ModuleType("settings.config")
    for name, value in config_values.items():
        setattr(config_module, name, value)
    settings_module.config = config_module  # type: ignore[attr-defined]
    logger_module = types.ModuleType("nb_log")
    logger_module.get_logger = lambda *_args, **_kwargs: _NullLogger()  # type: ignore[attr-defined]
    sys.modules["settings"] = settings_module
    sys.modules["settings.config"] = config_module
    sys.modules["nb_log"] = logger_module
    sys.path.insert(0, str(legacy_source_root))
    for name in (
        "tools.product_parser_utils",
        "tools.lxml_tool",
        "tools.Response",
        "tools",
    ):
        sys.modules.pop(name, None)
    try:
        response_module = importlib.import_module("tools.Response")
        parser_module = importlib.import_module("tools.product_parser_utils")
    except ImportError as exc:
        raise ControlledEvidenceError(
            "current legacy product parser could not be loaded from the source root"
        ) from exc
    for module in (response_module, parser_module):
        module_path = Path(str(getattr(module, "__file__", ""))).resolve()
        if not module_path.is_relative_to(legacy_source_root):
            raise ControlledEvidenceError(
                "loaded legacy parser module is outside the selected source root"
            )
    RnetResponse = response_module.RnetResponse
    parse_html_data = parser_module.parse_html_data
    validate_response = parser_module.validate_response

    class LegacyStatus:
        def __init__(self, code: int) -> None:
            self.code = int(code)

        def is_success(self) -> bool:
            return 200 <= self.code < 300

        def is_redirection(self) -> bool:
            return 300 <= self.code < 400

    def run(
        html: str,
        task: dict[str, Any],
        status_code: int = 200,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        original_response = types.SimpleNamespace(status=LegacyStatus(status_code))
        response = RnetResponse(original_response, html)
        validate_response(response)
        dimensions, result = parse_html_data(response, task)
        if not isinstance(dimensions, list) or not isinstance(result, dict):
            raise ControlledEvidenceError(
                "current legacy product parser returned an invalid projection"
            )
        return dimensions, result

    return run


def _load_current_legacy_collection_parser(
    legacy_source_root: Path,
) -> LegacyCollectionParser:
    legacy_source_root = legacy_source_root.resolve()
    config_values = _literal_legacy_config(legacy_source_root / "settings/config.py")
    try:
        import parsel  # noqa: F401
    except ImportError as exc:
        raise ControlledEvidenceError(
            "current legacy collection replay requires legacy-parity dependencies"
        ) from exc

    settings_module = types.ModuleType("settings")
    settings_module.__path__ = []  # type: ignore[attr-defined]
    config_module = types.ModuleType("settings.config")
    for name, value in config_values.items():
        setattr(config_module, name, value)
    settings_module.config = config_module  # type: ignore[attr-defined]
    logger_module = types.ModuleType("nb_log")
    logger_module.get_logger = lambda *_args, **_kwargs: _NullLogger()  # type: ignore[attr-defined]
    sys.modules["settings"] = settings_module
    sys.modules["settings.config"] = config_module
    sys.modules["nb_log"] = logger_module
    sys.path.insert(0, str(legacy_source_root))
    parser_modules = {
        "search": "tools.search_parser_utils",
        "search_hour": "tools.search_hour_parser_utils",
        "reviews": "tools.reviews_parser_utils",
        "category_asin_list": "tools.asin_list_parser_utils",
        "rank_list": "tools.rank_list_parser_utils",
    }
    for name in (
        *parser_modules.values(),
        "tools.lxml_tool",
        "tools.Response",
        "tools",
    ):
        sys.modules.pop(name, None)
    try:
        response_module = importlib.import_module("tools.Response")
        loaded = {
            kind: importlib.import_module(module_name)
            for kind, module_name in parser_modules.items()
        }
    except ImportError as exc:
        raise ControlledEvidenceError(
            "current legacy collection parsers could not be loaded"
        ) from exc
    for module in (response_module, *loaded.values()):
        module_path = Path(str(getattr(module, "__file__", ""))).resolve()
        if not module_path.is_relative_to(legacy_source_root):
            raise ControlledEvidenceError(
                "loaded legacy collection parser is outside the selected source root"
            )
    # The old search and rank parsers call eval on upstream text.  Preserve
    # literal semantics but remove code execution from controlled replay.
    for kind in ("search", "search_hour", "rank_list"):
        loaded[kind].eval = ast.literal_eval
    RnetResponse = response_module.RnetResponse

    class LegacyStatus:
        def __init__(self, code: int) -> None:
            self.code = int(code)

        def is_success(self) -> bool:
            return 200 <= self.code < 300

        def is_redirection(self) -> bool:
            return 300 <= self.code < 400

    def response(html: str, status_code: int) -> Any:
        original = types.SimpleNamespace(status=LegacyStatus(status_code))
        return RnetResponse(original, html)

    def run(
        kind: str,
        responses: list[tuple[str, int]],
        task: dict[str, Any],
    ) -> list[dict[str, Any]]:
        if kind not in loaded or not responses:
            raise ControlledEvidenceError("legacy collection replay input is invalid")
        module = loaded[kind]
        first_html, first_status = responses[0]
        first = response(first_html, first_status)
        if kind == "search":
            module.validate_response(
                first,
                task["keyword"],
                task["market_id"],
                task["turn_page"],
            )
            return module.parse_html_data(first_html, task)
        if kind == "search_hour":
            # The hourly legacy worker deliberately accepts an otherwise
            # healthy page without sponsored rows on its final (11th) attempt.
            # Controlled success replay must use that terminal acceptance
            # branch instead of incorrectly treating the same page as failure.
            module.validate_response(
                first,
                task["keyword"],
                task["market_id"],
                task["turn_page"],
                11,
                10,
            )
            return module.parse_html_data(first_html, task)
        if kind == "reviews":
            module.validate_response(first)
            return module.parse_html_data(first, task)
        if kind == "category_asin_list":
            module.validate_response(first, task["market_id"], task["category_id"])
            return module.parse_html_data(first, task)
        module.validate_response(first)
        _has_next, items, next_params = module.parse_html_data(first, task)
        for html, status_code in responses[1:]:
            continued = response(html, status_code)
            module.validate_response(continued)
            items.extend(module.parse_items(continued, task, next_params))
        return items

    return run


def _load_current_legacy_merchant_parser(
    legacy_source_root: Path,
) -> LegacyMerchantParser:
    legacy_source_root = legacy_source_root.resolve()
    config_values = _literal_legacy_config(legacy_source_root / "settings/config.py")
    try:
        import parsel  # noqa: F401
    except ImportError as exc:
        raise ControlledEvidenceError(
            "current legacy merchant replay requires legacy-parity dependencies"
        ) from exc

    settings_module = types.ModuleType("settings")
    settings_module.__path__ = []  # type: ignore[attr-defined]
    config_module = types.ModuleType("settings.config")
    for name, value in config_values.items():
        setattr(config_module, name, value)
    settings_module.config = config_module  # type: ignore[attr-defined]
    logger_module = types.ModuleType("nb_log")
    logger_module.get_logger = lambda *_args, **_kwargs: _NullLogger()  # type: ignore[attr-defined]
    sys.modules["settings"] = settings_module
    sys.modules["settings.config"] = config_module
    sys.modules["nb_log"] = logger_module
    sys.path.insert(0, str(legacy_source_root))
    parser_modules = {
        "merchant": "tools.merchant_parser_utils",
        "merchant_home": "tools.merchant_home_parser_utils",
        "merchant_products": "tools.merchant_products_parser_utils",
    }
    for name in (
        *parser_modules.values(),
        "tools.lxml_tool",
        "tools.Response",
        "tools",
    ):
        sys.modules.pop(name, None)
    try:
        response_module = importlib.import_module("tools.Response")
        loaded = {
            kind: importlib.import_module(module_name)
            for kind, module_name in parser_modules.items()
        }
    except ImportError as exc:
        raise ControlledEvidenceError(
            "current legacy merchant parsers could not be loaded"
        ) from exc
    for module in (response_module, *loaded.values()):
        module_path = Path(str(getattr(module, "__file__", ""))).resolve()
        if not module_path.is_relative_to(legacy_source_root):
            raise ControlledEvidenceError(
                "loaded legacy merchant parser is outside the selected source root"
            )
    loaded["merchant_products"].eval = ast.literal_eval
    # The current parser writes every explicit no-result page to ``./html``
    # before raising its terminal NoPageError.  Controlled replay must not leak
    # raw responses or depend on the caller's working directory, so retain the
    # successful debug-write semantics with an in-memory discard sink.
    loaded["merchant_products"].open = (  # type: ignore[attr-defined]
        lambda *_args, **_kwargs: io.StringIO()
    )
    RnetResponse = response_module.RnetResponse

    class LegacyStatus:
        def __init__(self, code: int) -> None:
            self.code = int(code)

        def is_success(self) -> bool:
            return 200 <= self.code < 300

        def is_redirection(self) -> bool:
            return 300 <= self.code < 400

        def __str__(self) -> str:
            return str(self.code)

    def run(
        kind: str,
        html: str,
        status_code: int,
        task: dict[str, Any],
        url: str,
    ) -> dict[str, Any] | list[dict[str, Any]]:
        if kind not in loaded:
            raise ControlledEvidenceError("legacy merchant replay kind is invalid")
        original = types.SimpleNamespace(
            status=LegacyStatus(status_code),
            url=url,
        )
        response = RnetResponse(original, html)
        module = loaded[kind]
        if kind == "merchant":
            module.validate_response(response, task["seller_id"])
            return module.parse_html_data(
                response,
                task["seller_id"],
                task["market_id"],
                url,
            )
        if kind == "merchant_home":
            module.validate_response(
                response,
                task["seller_id"],
                task["market_id"],
            )
            return module.parse_html_data(html, task)
        module.validate_response(
            response,
            task["seller_id"],
            task["market_id"],
            task["page"],
        )
        return module.parse_html_data(html, task)

    return run


def _load_pair(html_path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
    json_path = html_path.with_suffix(".json")
    if not json_path.is_file():
        return None
    try:
        payload = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or not isinstance(
        payload.get("response_data"), dict
    ):
        return None
    dimensions = payload.get("skus_items")
    if not isinstance(dimensions, list) or not all(
        isinstance(row, dict) for row in dimensions
    ):
        return None
    return payload, dimensions


def _audit_pair(
    *,
    html_path: Path,
    archive_root: Path,
    payload: dict[str, Any],
    legacy_dimensions: list[dict[str, Any]],
    legacy_parser: LegacyParser | None,
) -> dict[str, Any]:
    archived_legacy = dict(payload["response_data"])
    asin = str(archived_legacy.get("asin") or "").upper()
    legacy_marketplace = str(archived_legacy.get("marketplace_id") or "")
    market = AMAZON_ID_TO_MARKETPLACE.get(legacy_marketplace)
    postal_code = str(archived_legacy.get("zipcode") or "")
    product_url = payload.get("url")
    if market is None or len(asin) != 10 or not isinstance(product_url, str):
        raise ControlledEvidenceError(
            "archived product pair lacks a supported marketplace, ASIN, or URL"
        )
    task_id: object = ""
    if legacy_dimensions:
        task_id = legacy_dimensions[0].get("task_id", "")
    html = html_path.read_text(encoding="utf-8", errors="replace")
    legacy = archived_legacy
    expected_dimensions = legacy_dimensions
    archived_output_differences: list[str] = []
    if legacy_parser is not None:
        expected_dimensions, legacy = legacy_parser(
            html,
            {
                "id": task_id,
                "market_id": legacy_marketplace,
                "asin": asin,
                "post_code": postal_code,
            },
        )
        archived_output_differences = _differences(
            _normalize_comparable(
                {"result": archived_legacy, "dimension_rows": legacy_dimensions}
            ),
            _normalize_comparable(
                {"result": legacy, "dimension_rows": expected_dimensions}
            ),
            path="$.archived_output",
        )
    current = parse_product_html(
        html,
        asin=asin,
        product_url=product_url,
        marketplace_id=market.id,
        postal_code=postal_code,
        # The archived parser preserved the input task-id type.  The live V2
        # bridge subsequently serializes it according to the VARCHAR DB column.
        task_id=task_id,  # type: ignore[arg-type]
    )
    comparable_current = {key: current.get(key) for key in legacy}
    differences = _differences(
        _normalize_comparable(legacy),
        _normalize_comparable(comparable_current),
    )

    current_dimensions = current.get("dimension_items")
    if not isinstance(current_dimensions, list):
        current_dimensions = []
    dimension_keys = sorted(
        {
            str(key)
            for row in expected_dimensions
            for key in row
        }
    )
    comparable_dimensions = [
        {key: row.get(key) for key in dimension_keys}
        for row in current_dimensions
        if isinstance(row, dict)
    ]
    differences.extend(
        _differences(
            _normalize_comparable(expected_dimensions),
            _normalize_comparable(comparable_dimensions),
            path="$.dimension_rows",
        )
    )
    identity = str(html_path.relative_to(archive_root))
    return {
        "sample_identity_sha256": hashlib.sha256(identity.encode("utf-8")).hexdigest(),
        "html_sha256": hashlib.sha256(html.encode("utf-8")).hexdigest(),
        "archived_legacy_output_sha256": _digest(
            {"result": archived_legacy, "dimension_rows": legacy_dimensions}
        ),
        "legacy_output_sha256": _digest(
            {"result": legacy, "dimension_rows": expected_dimensions}
        ),
        "v2_output_sha256": _digest(
            {"result": comparable_current, "dimension_rows": comparable_dimensions}
        ),
        "marketplace_code": market.id,
        "legacy_field_count": len(legacy),
        "dimension_row_count": len(expected_dimensions),
        "archived_output_drift_count": len(archived_output_differences),
        "archived_output_drift_paths": archived_output_differences[:100],
        "status": "passed" if not differences else "failed",
        "difference_count": len(differences),
        "differences": differences[:100],
    }


def audit_archive(
    *,
    archive_root: Path,
    project_root: Path,
    legacy_source_root: Path | None = None,
) -> dict[str, Any]:
    archive_root = archive_root.resolve()
    if not archive_root.is_dir():
        raise ControlledEvidenceError("archive root must be an existing directory")
    legacy_parser = (
        _load_current_legacy_parser(legacy_source_root)
        if legacy_source_root is not None
        else None
    )
    samples: list[dict[str, Any]] = []
    ignored_pair_count = 0
    for html_path in sorted(archive_root.rglob("*.html")):
        loaded = _load_pair(html_path)
        if loaded is None:
            if html_path.with_suffix(".json").exists():
                ignored_pair_count += 1
            continue
        payload, dimensions = loaded
        samples.append(
            _audit_pair(
                html_path=html_path,
                archive_root=archive_root,
                payload=payload,
                legacy_dimensions=dimensions,
                legacy_parser=legacy_parser,
            )
        )
    if not samples:
        raise ControlledEvidenceError(
            "archive contains no product HTML/legacy-output pairs"
        )

    source_hashes = {
        "v2_product_parser_sha256": _file_digest(
            project_root / "src/amazon_crawler/plugins/amazon_parser.py"
        )
    }
    if legacy_source_root is not None:
        legacy_source_root = legacy_source_root.resolve()
        for key, relative in (
            ("legacy_product_parser_sha256", Path("tools/product_parser_utils.py")),
            ("legacy_parser_helpers_sha256", Path("tools/lxml_tool.py")),
        ):
            path = legacy_source_root / relative
            if not path.is_file():
                raise ControlledEvidenceError(
                    "legacy source root does not contain the expected parser files"
                )
            source_hashes[key] = _file_digest(path)

    failed = [sample for sample in samples if sample["status"] != "passed"]
    drifted = [
        sample for sample in samples if sample["archived_output_drift_count"] > 0
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "mode": (
            "current_legacy_source_same_response_replay"
            if legacy_parser is not None
            else "archived_legacy_output_same_response_replay"
        ),
        "final_controlled_matrix_satisfied": False,
        "archive_root_sha256": hashlib.sha256(
            str(archive_root).encode("utf-8")
        ).hexdigest(),
        "source_hashes": source_hashes,
        "sample_count": len(samples),
        "passed_sample_count": len(samples) - len(failed),
        "failed_sample_count": len(failed),
        "archived_output_drift_sample_count": len(drifted),
        "ignored_non_product_pair_count": ignored_pair_count,
        "status": "passed" if not failed else "failed",
        "samples": samples,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Replay archived product HTML against V2 and compare it with the "
            "paired legacy parser output. Performs no network request and no write "
            "to the legacy project."
        )
    )
    parser.add_argument("archive_root", type=Path)
    parser.add_argument("--legacy-source-root", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    project_root = Path(__file__).resolve().parents[1]
    try:
        report = audit_archive(
            archive_root=args.archive_root,
            project_root=project_root,
            legacy_source_root=args.legacy_source_root,
        )
        _atomic_write_outside_project(
            args.output,
            report,
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
            else "archived product parity audit could not be completed"
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
                "ok": report["status"] == "passed",
                "output": str(args.output.resolve()),
                "sample_count": report["sample_count"],
                "passed_sample_count": report["passed_sample_count"],
                "failed_sample_count": report["failed_sample_count"],
                "final_controlled_matrix_satisfied": False,
                "network_requests": 0,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
