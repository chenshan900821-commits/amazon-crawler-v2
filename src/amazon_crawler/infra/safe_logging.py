from __future__ import annotations

import json
import logging
import os
import re
from datetime import UTC, datetime
from typing import Any

_URL_WITH_QUERY = re.compile(r"(https?://[^\s\"?]+)\?[^\s\"]+")
_URL_WITH_USERINFO = re.compile(r"(https?://)[^/@\s\"]+@")
_SENSITIVE_KEY = re.compile(
    r"cookie|token|secret|password|authorization|proxy_url|signing_key",
    re.IGNORECASE,
)
_EVENT_LOGGER_NAME = "amazon_crawler.events"


class RedactHTTPURLSecrets(logging.Filter):
    """Remove query strings and URL userinfo before HTTP client logs propagate."""

    def filter(self, record: logging.LogRecord) -> bool:
        rendered = record.getMessage()
        rendered = _URL_WITH_QUERY.sub(r"\1?<redacted>", rendered)
        rendered = _URL_WITH_USERINFO.sub(r"\1<redacted>@", rendered)
        record.msg = rendered
        record.args = ()
        return True


def install_http_log_redaction() -> None:
    logger = logging.getLogger("httpx")
    if any(isinstance(item, RedactHTTPURLSecrets) for item in logger.filters):
        return
    logger.addFilter(RedactHTTPURLSecrets())


def _safe_log_value(key: str, value: Any, *, depth: int = 0) -> Any:
    if _SENSITIVE_KEY.search(key):
        return "<redacted>"
    if depth >= 3:
        return "<truncated>"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        rendered = _URL_WITH_QUERY.sub(r"\1?<redacted>", value)
        rendered = _URL_WITH_USERINFO.sub(r"\1<redacted>@", rendered)
        return rendered[:512]
    if isinstance(value, dict):
        return {
            str(item_key)[:80]: _safe_log_value(
                str(item_key), item_value, depth=depth + 1
            )
            for item_key, item_value in list(value.items())[:50]
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [
            _safe_log_value(key, item, depth=depth + 1) for item in list(value)[:50]
        ]
    return type(value).__name__


def _event_logger() -> logging.Logger:
    logger = logging.getLogger(_EVENT_LOGGER_NAME)
    level_name = os.getenv("CRAWLER_LOG_LEVEL", "INFO").strip().upper()
    level = getattr(logging, level_name, logging.INFO)
    logger.setLevel(level)
    logger.propagate = False
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    install_http_log_redaction()
    return logger


def emit_structured_event(
    event: str,
    *,
    level: int = logging.INFO,
    **fields: Any,
) -> None:
    """Emit one bounded JSON event without serializing runtime secrets."""
    payload = {
        "timestamp": datetime.now(UTC).isoformat(),
        "event": event[:100],
        **{
            str(key)[:80]: _safe_log_value(str(key), value)
            for key, value in fields.items()
        },
    }
    _event_logger().log(
        level,
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
    )
