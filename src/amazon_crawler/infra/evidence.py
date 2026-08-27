from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from amazon_crawler.domain.models import CrawlFailure


PUBLIC_RESPONSE_HEADERS = {
    "content-language",
    "content-length",
    "content-type",
}


def public_response_metadata(
    *,
    url: str,
    status_code: int,
    headers: dict[str, str],
) -> dict[str, object]:
    """Return response evidence that is safe to persist and expose.

    Amazon may return ``Set-Cookie`` and request-correlating values on otherwise
    successful pages.  Evidence only needs the canonical location, status and a
    small content-description allowlist; query strings, fragments and every
    other response header stay in memory.
    """

    parsed = urlsplit(url)
    source_url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    public_headers = {
        str(key).lower(): str(value)
        for key, value in headers.items()
        if str(key).lower() in PUBLIC_RESPONSE_HEADERS
    }
    return {
        "source_url": source_url,
        "http_status": int(status_code),
        "response_headers": public_headers,
    }


class EvidenceStore:
    def __init__(self, root: Path, enabled: bool) -> None:
        self._root = root
        self._enabled = enabled

    def save_html(self, *, job_id: str, item_id: str, html: str) -> dict[str, str | int | bool]:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", job_id):
            raise ValueError("job_id is not safe for evidence storage")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", item_id):
            raise ValueError("item_id is not safe for evidence storage")
        raw = html.encode("utf-8")
        digest = hashlib.sha256(raw).hexdigest()
        evidence: dict[str, str | int | bool] = {
            "sha256": digest,
            "bytes": len(raw),
            "captured": self._enabled,
        }
        if not self._enabled:
            return evidence
        if self._root.is_symlink():
            raise ValueError("evidence root must not be a symbolic link")
        directory = self._root / job_id
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if directory.is_symlink():
            raise ValueError("evidence job directory must not be a symbolic link")
        directory.chmod(0o700)
        # Content-addressed names preserve the evidence from every retry instead
        # of letting a later response overwrite an earlier attempt.
        filename = f"{item_id}-{digest[:16]}.html"
        path = directory / filename
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb", closefd=False) as handle:
                handle.write(raw)
        finally:
            os.close(descriptor)
        path.chmod(0o600)
        evidence["artifact_ref"] = f"{job_id}/{filename}"
        return evidence

    def failure_details(
        self,
        *,
        job_id: str,
        item_id: str,
        html: str,
        http_status: int,
    ) -> dict[str, object]:
        return {
            "http_status": http_status,
            "evidence": self.save_html(
                job_id=job_id,
                item_id=item_id,
                html=html,
            ),
        }

    def attach_failure(
        self,
        failure: CrawlFailure,
        *,
        job_id: str,
        item_id: str,
        html: str,
        http_status: int,
    ) -> CrawlFailure:
        return CrawlFailure(
            code=failure.code,
            message=failure.message,
            retryable=failure.retryable,
            details={
                **failure.details,
                **self.failure_details(
                    job_id=job_id,
                    item_id=item_id,
                    html=html,
                    http_status=http_status,
                ),
            },
        )
