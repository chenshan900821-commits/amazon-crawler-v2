#!/usr/bin/env python3
"""Inspect legacy runtime target boundaries without exposing configuration values.

This script deliberately parses ``settings/config.py`` as syntax instead of
importing it.  It reports only target classes and presence metadata; hosts,
ports, credentials, cookies, paths, query strings and complete URLs are never
printed.
"""

from __future__ import annotations

import argparse
import ast
import ipaddress
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


TARGET_NAMES = {
    "MYSQL_URL",
    "REDIS_HOST",
    "REDIS_PORT",
    "REDIS_DB",
    "REDIS_PASSWORD",
    "REDIS_COOKIE_IP_PORTS",
    "REDIS_COOKIE_DB",
    "REDIS_COOKIE_DB_HW",
    "REDIS_COOKIE_PASS",
    "PROXY_EXTRACT_API_QGHW",
    "PROXY_EXTRACT_API_QG",
    "PROXY_EXTRACT_API_QGAL",
    "PROXY_EXTRACT_API_JLAL",
    "MERCHANT_COOKIE",
    "COOKIE_HEADERS",
    "MERCHANT_HEADERS",
}

SECRET_NAMES = {
    "REDIS_PASSWORD",
    "REDIS_COOKIE_PASS",
    "MERCHANT_COOKIE",
    "COOKIE_HEADERS",
    "MERCHANT_HEADERS",
}


def _literal_assignments(path: Path) -> dict[str, Any]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    values: dict[str, Any] = {}
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        if isinstance(node, ast.Assign):
            if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
                continue
            name = node.targets[0].id
            value_node = node.value
        else:
            if not isinstance(node.target, ast.Name) or node.value is None:
                continue
            name = node.target.id
            value_node = node.value
        if name not in TARGET_NAMES:
            continue
        try:
            values[name] = ast.literal_eval(value_node)
        except (ValueError, TypeError):
            values[name] = _Unresolved(type(value_node).__name__)
    return values


class _Unresolved:
    def __init__(self, expression_type: str) -> None:
        self.expression_type = expression_type


def _host_scope(host: str | None) -> str:
    if not host:
        return "unresolved"
    normalized = host.strip().strip("[]").lower()
    if normalized in {"localhost", "localhost.localdomain"}:
        return "loopback"
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        return "hostname"
    if address.is_loopback:
        return "loopback"
    if address.is_private:
        return "private_network"
    if address.is_link_local:
        return "link_local"
    return "public_network"


def _url_metadata(value: str) -> dict[str, Any]:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return {"configured": bool(value), "target_scope": "unresolved"}
    return {
        "configured": bool(value),
        "scheme": parsed.scheme or "unresolved",
        "target_scope": _host_scope(parsed.hostname),
    }


def _host_list_metadata(value: str) -> dict[str, Any]:
    scopes: list[str] = []
    for token in re.split(r"[,;\s]+", value.strip()):
        if not token:
            continue
        candidate = token
        if "://" not in candidate:
            candidate = f"redis://{candidate}"
        try:
            scopes.append(_host_scope(urlsplit(candidate).hostname))
        except ValueError:
            scopes.append("unresolved")
    return {
        "configured": bool(value),
        "target_count": len(scopes),
        "target_scopes": sorted(set(scopes)) or ["unresolved"],
    }


def inspect(path: Path) -> dict[str, Any]:
    values = _literal_assignments(path)
    report: dict[str, Any] = {
        "source": "legacy_settings_ast",
        "imports_executed": False,
        "values_disclosed": False,
        "targets": {},
    }
    for name in sorted(TARGET_NAMES):
        if name not in values:
            report["targets"][name] = {"configured": False}
            continue
        value = values[name]
        if isinstance(value, _Unresolved):
            report["targets"][name] = {
                "configured": True,
                "resolved": False,
                "expression_type": value.expression_type,
            }
        elif name in SECRET_NAMES:
            report["targets"][name] = {
                "configured": bool(value),
                "resolved": True,
                "value_type": type(value).__name__,
            }
        elif name == "MYSQL_URL" and isinstance(value, str):
            report["targets"][name] = _url_metadata(value)
        elif name == "REDIS_HOST" and isinstance(value, str):
            report["targets"][name] = {
                "configured": bool(value),
                "target_scope": _host_scope(value),
            }
        elif name == "REDIS_COOKIE_IP_PORTS" and isinstance(value, str):
            report["targets"][name] = _host_list_metadata(value)
        elif name.startswith("PROXY_EXTRACT_API_") and isinstance(value, str):
            report["targets"][name] = _url_metadata(value)
        else:
            report["targets"][name] = {
                "configured": value is not None,
                "resolved": True,
                "value_type": type(value).__name__,
            }
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "config",
        nargs="?",
        type=Path,
        default=Path(__file__).resolve().parents[2] / "settings" / "config.py",
    )
    args = parser.parse_args()
    print(json.dumps(inspect(args.config.resolve()), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
