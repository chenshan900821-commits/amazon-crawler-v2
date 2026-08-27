from __future__ import annotations

import ast
import ipaddress
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit


class _Unresolved:
    pass


UNRESOLVED = _Unresolved()


LEGACY_PROXY_ROUTES: dict[str, tuple[str, str, str]] = {
    "qg": ("PROXY_EXTRACT_API_QG", "username_qg", "password_qg"),
    "qghw": ("PROXY_EXTRACT_API_QGHW", "username_qghw", "password_qghw"),
    "qgal": ("PROXY_EXTRACT_API_QGAL", "username_qgal", "password_qgal"),
    "jlal": ("PROXY_EXTRACT_API_JLAL", "username_jlal", "password_jlal"),
}


def _safe_eval(node: ast.AST, values: dict[str, Any]) -> Any:
    """Evaluate data-only legacy expressions without importing legacy code."""

    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        return values.get(node.id, UNRESOLVED)
    if isinstance(node, ast.List):
        items = [_safe_eval(item, values) for item in node.elts]
        return UNRESOLVED if UNRESOLVED in items else items
    if isinstance(node, ast.Tuple):
        items = [_safe_eval(item, values) for item in node.elts]
        return UNRESOLVED if UNRESOLVED in items else tuple(items)
    if isinstance(node, ast.Set):
        items = [_safe_eval(item, values) for item in node.elts]
        return UNRESOLVED if UNRESOLVED in items else set(items)
    if isinstance(node, ast.Dict):
        keys = [_safe_eval(item, values) for item in node.keys]
        items = [_safe_eval(item, values) for item in node.values]
        if UNRESOLVED in keys or UNRESOLVED in items:
            return UNRESOLVED
        try:
            return dict(zip(keys, items, strict=True))
        except (TypeError, ValueError):
            return UNRESOLVED
    if isinstance(node, ast.JoinedStr):
        chunks: list[str] = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                chunks.append(value.value)
                continue
            if isinstance(value, ast.FormattedValue):
                rendered = _safe_eval(value.value, values)
                if rendered is UNRESOLVED:
                    return UNRESOLVED
                chunks.append(str(rendered))
                continue
            return UNRESOLVED
        return "".join(chunks)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _safe_eval(node.left, values)
        right = _safe_eval(node.right, values)
        if left is UNRESOLVED or right is UNRESOLVED:
            return UNRESOLVED
        try:
            return left + right
        except TypeError:
            return UNRESOLVED
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        operand = _safe_eval(node.operand, values)
        if not isinstance(operand, (int, float)):
            return UNRESOLVED
        return operand if isinstance(node.op, ast.UAdd) else -operand
    if isinstance(node, ast.Subscript):
        value = _safe_eval(node.value, values)
        index = _safe_eval(node.slice, values)
        if value is UNRESOLVED or index is UNRESOLVED:
            return UNRESOLVED
        try:
            return value[index]
        except (KeyError, IndexError, TypeError):
            return UNRESOLVED
    if isinstance(node, ast.Call):
        args = [_safe_eval(value, values) for value in node.args]
        if UNRESOLVED in args or node.keywords:
            return UNRESOLVED
        if isinstance(node.func, ast.Name) and node.func.id in {"str", "int", "float"}:
            if len(args) != 1:
                return UNRESOLVED
            try:
                return {"str": str, "int": int, "float": float}[node.func.id](args[0])
            except (TypeError, ValueError):
                return UNRESOLVED
        if (
            isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "random"
        ):
            if node.func.attr == "choice" and len(args) == 1 and args[0]:
                return args[0][0]
            if (
                node.func.attr == "randint"
                and len(args) == 2
                and all(isinstance(value, int) for value in args)
            ):
                return args[0]
        return UNRESOLVED
    return UNRESOLVED


def _read_assignments(path: Path) -> dict[str, Any]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    values: dict[str, Any] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            value_node = node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            target = node.target
            value_node = node.value
        else:
            continue
        if not isinstance(target, ast.Name):
            continue
        resolved = _safe_eval(value_node, values)
        if resolved is not UNRESOLVED:
            values[target.id] = resolved
    return values


def target_scope(host: str | None) -> str:
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


def _url_scope(value: str | None) -> str:
    if not value:
        return "unresolved"
    try:
        return target_scope(urlsplit(value).hostname)
    except ValueError:
        return "unresolved"


def _redis_url(host_port: str, password: str, database: int) -> str:
    candidate = host_port.strip()
    parsed = urlsplit(candidate if "://" in candidate else f"redis://{candidate}")
    if not parsed.hostname or parsed.port is None:
        raise ValueError("legacy Redis target must contain one host and port")
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    credentials = f":{quote(password, safe='')}@" if password else ""
    return f"redis://{credentials}{host}:{parsed.port}/{int(database)}"


@dataclass(frozen=True, slots=True)
class LegacyRuntimeConfig:
    source_path: Path
    mysql_url: str | None = field(repr=False)
    result_redis_url: str | None = field(repr=False)
    cookie_redis_url: str | None = field(repr=False)
    cookie_redis_overseas_url: str | None = field(repr=False)
    proxy_extract_url: str | None = field(repr=False)
    proxy_username: str | None = field(repr=False)
    proxy_password: str | None = field(repr=False)
    proxy_route: str
    merchant_cookie: str | None = field(repr=False)
    target_scopes: dict[str, str]

    def public_report(self) -> dict[str, Any]:
        return {
            "source": "legacy_settings_ast",
            "imports_executed": False,
            "values_disclosed": False,
            "configured": {
                "mysql": self.mysql_url is not None,
                "result_redis": self.result_redis_url is not None,
                "cookie_redis": self.cookie_redis_url is not None,
                "cookie_redis_overseas": self.cookie_redis_overseas_url is not None,
                "proxy_extract": self.proxy_extract_url is not None,
                "proxy_credentials": bool(self.proxy_username and self.proxy_password),
                "merchant_cookie": self.merchant_cookie is not None,
            },
            "proxy_route": self.proxy_route,
            "target_scopes": dict(self.target_scopes),
        }


def load_legacy_runtime_config(
    path: Path,
    *,
    allow_external_cookie_read: bool = False,
    allow_external_proxy_api: bool = False,
    proxy_route: str = "qg",
) -> LegacyRuntimeConfig:
    path = path.resolve()
    values = _read_assignments(path)
    normalized_proxy_route = proxy_route.strip().lower()
    try:
        proxy_url_name, proxy_username_name, proxy_password_name = (
            LEGACY_PROXY_ROUTES[normalized_proxy_route]
        )
    except KeyError as exc:
        allowed = ", ".join(sorted(LEGACY_PROXY_ROUTES))
        raise ValueError(f"legacy proxy route must be one of: {allowed}") from exc

    mysql_url = values.get("MYSQL_URL")
    if not isinstance(mysql_url, str):
        mysql_url = None

    result_redis_url = None
    redis_host = values.get("REDIS_HOST")
    redis_port = values.get("REDIS_PORT")
    redis_password = values.get("REDIS_PASSWORD", "")
    redis_db = values.get("REDIS_DB")
    if (
        isinstance(redis_host, str)
        and isinstance(redis_port, int)
        and isinstance(redis_password, str)
        and isinstance(redis_db, int)
    ):
        result_redis_url = _redis_url(
            f"{redis_host}:{redis_port}", redis_password, redis_db
        )

    cookie_redis_url = None
    cookie_redis_overseas_url = None
    cookie_target = values.get("REDIS_COOKIE_IP_PORTS")
    cookie_password = values.get("REDIS_COOKIE_PASS", "")
    cookie_db = values.get("REDIS_COOKIE_DB")
    cookie_overseas_db = values.get("REDIS_COOKIE_DB_HW")
    if isinstance(cookie_target, str) and isinstance(cookie_password, str):
        if isinstance(cookie_db, int):
            candidate = _redis_url(cookie_target, cookie_password, cookie_db)
            if _url_scope(candidate) == "loopback" or allow_external_cookie_read:
                cookie_redis_url = candidate
        if isinstance(cookie_overseas_db, int):
            candidate = _redis_url(cookie_target, cookie_password, cookie_overseas_db)
            if _url_scope(candidate) == "loopback" or allow_external_cookie_read:
                cookie_redis_overseas_url = candidate

    proxy_extract_url = values.get(proxy_url_name)
    if not isinstance(proxy_extract_url, str):
        proxy_extract_url = None
    if proxy_extract_url and _url_scope(proxy_extract_url) != "loopback":
        if not allow_external_proxy_api:
            proxy_extract_url = None
    proxy_username = values.get(proxy_username_name)
    proxy_password = values.get(proxy_password_name)
    if not isinstance(proxy_username, str) or not proxy_username:
        proxy_username = None
    if not isinstance(proxy_password, str) or not proxy_password:
        proxy_password = None
    if proxy_extract_url is None:
        proxy_username = None
        proxy_password = None

    merchant_cookie_value = values.get("MERCHANT_COOKIE")
    merchant_cookie = None
    if isinstance(merchant_cookie_value, dict) and merchant_cookie_value:
        merchant_cookie = json.dumps(
            merchant_cookie_value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
    elif isinstance(merchant_cookie_value, str) and merchant_cookie_value:
        merchant_cookie = merchant_cookie_value

    scopes = {
        "mysql": _url_scope(mysql_url),
        "result_redis": _url_scope(result_redis_url),
        "cookie_redis": _url_scope(
            cookie_redis_url
            or (
                _redis_url(cookie_target, cookie_password, cookie_db)
                if isinstance(cookie_target, str)
                and isinstance(cookie_password, str)
                and isinstance(cookie_db, int)
                else None
            )
        ),
        "proxy_extract": _url_scope(
            values.get(proxy_url_name)
            if isinstance(values.get(proxy_url_name), str)
            else None
        ),
    }
    if mysql_url and scopes["mysql"] != "loopback":
        raise ValueError("legacy MySQL writes are allowed only for a loopback target")
    if result_redis_url and scopes["result_redis"] != "loopback":
        raise ValueError("legacy result Redis writes are allowed only for a loopback target")

    return LegacyRuntimeConfig(
        source_path=path,
        mysql_url=mysql_url,
        result_redis_url=result_redis_url,
        cookie_redis_url=cookie_redis_url,
        cookie_redis_overseas_url=cookie_redis_overseas_url,
        proxy_extract_url=proxy_extract_url,
        proxy_username=proxy_username,
        proxy_password=proxy_password,
        proxy_route=normalized_proxy_route,
        merchant_cookie=merchant_cookie,
        target_scopes=scopes,
    )
