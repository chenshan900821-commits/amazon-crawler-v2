from __future__ import annotations

import logging
import re

_URL_WITH_QUERY = re.compile(r"(https?://[^\s\"?]+)\?[^\s\"]+")
_URL_WITH_USERINFO = re.compile(r"(https?://)[^/@\s\"]+@")


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
