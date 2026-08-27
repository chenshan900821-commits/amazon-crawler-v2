from __future__ import annotations

import json
import hashlib
import stat
import tempfile
import unittest
from pathlib import Path

from amazon_crawler.infra.evidence import EvidenceStore, public_response_metadata


class EvidenceStoreTests(unittest.TestCase):
    def test_public_response_metadata_cannot_persist_cookie_or_query_secrets(self) -> None:
        metadata = public_response_metadata(
            url=(
                "https://www.amazon.com/dp/B000000001"
                "?token=secret-value&session-id=private#fragment"
            ),
            status_code=200,
            headers={
                "Content-Type": "text/html",
                "Content-Language": "en-US",
                "Set-Cookie": "session=secret-value",
                "X-Amz-Rid": "request-correlator",
            },
        )

        self.assertEqual(
            metadata,
            {
                "source_url": "https://www.amazon.com/dp/B000000001",
                "http_status": 200,
                "response_headers": {
                    "content-type": "text/html",
                    "content-language": "en-US",
                },
            },
        )
        rendered = json.dumps(metadata, sort_keys=True)
        self.assertNotIn("secret-value", rendered)
        self.assertNotIn("set-cookie", rendered.lower())

    def test_retries_keep_distinct_content_addressed_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            store = EvidenceStore(Path(tempdir), enabled=True)
            first = store.save_html(job_id="job-1", item_id="item-1", html="first")
            second = store.save_html(job_id="job-1", item_id="item-1", html="second")

            self.assertNotEqual(first["sha256"], second["sha256"])
            self.assertNotEqual(first["artifact_ref"], second["artifact_ref"])
            self.assertFalse(Path(str(first["artifact_ref"])).is_absolute())
            self.assertEqual((Path(tempdir) / str(first["artifact_ref"])).read_text(), "first")
            self.assertEqual((Path(tempdir) / str(second["artifact_ref"])).read_text(), "second")
            self.assertNotIn(str(Path(tempdir)), json.dumps(first))
            self.assertEqual(
                stat.S_IMODE(
                    (Path(tempdir) / str(first["artifact_ref"])).stat().st_mode
                ),
                0o600,
            )
            self.assertEqual(
                stat.S_IMODE((Path(tempdir) / "job-1").stat().st_mode),
                0o700,
            )

    def test_capture_rejects_unsafe_storage_identifiers(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            store = EvidenceStore(Path(tempdir), enabled=True)
            for job_id, item_id in (("../job", "item-1"), ("job-1", "../item")):
                with self.assertRaisesRegex(ValueError, "not safe"):
                    store.save_html(job_id=job_id, item_id=item_id, html="fixture")

    def test_capture_refuses_a_symlinked_artifact_target(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "evidence"
            directory = root / "job-1"
            directory.mkdir(parents=True)
            raw = b"fixture"
            digest = hashlib.sha256(raw).hexdigest()
            target = Path(tempdir) / "outside.html"
            target.write_text("unchanged")
            (directory / f"item-1-{digest[:16]}.html").symlink_to(target)

            with self.assertRaises(OSError):
                EvidenceStore(root, enabled=True).save_html(
                    job_id="job-1",
                    item_id="item-1",
                    html="fixture",
                )

            self.assertEqual(target.read_text(), "unchanged")

    def test_disabled_capture_keeps_hash_without_writing_html(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            evidence = EvidenceStore(root, enabled=False).save_html(
                job_id="job-1",
                item_id="item-1",
                html="not persisted",
            )

            self.assertFalse(evidence["captured"])
            self.assertNotIn("artifact_ref", evidence)
            self.assertEqual(list(root.rglob("*.html")), [])


if __name__ == "__main__":
    unittest.main()
