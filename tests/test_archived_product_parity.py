from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from amazon_crawler.plugins.amazon_parser import parse_product_html
from scripts.audit_archived_product_parity import audit_archive


class ArchivedProductParityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.project_root = Path(__file__).resolve().parents[1]
        self.fixture = self.project_root / "tests/fixtures/product.html"

    def _pair(self, directory: Path, *, mutate: bool = False) -> None:
        html = self.fixture.read_text(encoding="utf-8")
        expected = parse_product_html(
            html,
            asin="B000000001",
            product_url="https://www.amazon.com/dp/B000000001",
            marketplace_id="US",
            postal_code="10001",
            task_id=41,  # type: ignore[arg-type]
        )
        dimensions = expected.pop("dimension_items")
        old_keys = {
            key: value
            for key, value in expected.items()
            if key not in {
                "schema_version",
                "parser_version",
                "quality",
                "marketplace_code",
                "currency",
                "availability",
                "seller",
                "rating",
                "rating_count",
                "image_url",
                "parent_asin",
                "best_sellers_rank",
                "detail_rows",
                "canonical_url",
            }
        }
        if mutate:
            old_keys["title"] = "different historical output"
        (directory / "private-product-id.html").write_text(html, encoding="utf-8")
        (directory / "private-product-id.json").write_text(
            json.dumps(
                {
                    "url": "https://www.amazon.com/dp/B000000001",
                    "response_data": old_keys,
                    "skus_items": dimensions,
                }
            ),
            encoding="utf-8",
        )

    def test_replays_same_response_without_exposing_archive_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            archive = Path(tempdir)
            self._pair(archive)
            report = audit_archive(
                archive_root=archive,
                project_root=self.project_root,
            )

        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["sample_count"], 1)
        self.assertFalse(report["final_controlled_matrix_satisfied"])
        self.assertNotIn("private-product-id", json.dumps(report, sort_keys=True))

    def test_reports_field_path_for_a_real_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            archive = Path(tempdir)
            self._pair(archive, mutate=True)
            report = audit_archive(
                archive_root=archive,
                project_root=self.project_root,
            )

        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["samples"][0]["differences"], ["$.title"])


if __name__ == "__main__":
    unittest.main()
