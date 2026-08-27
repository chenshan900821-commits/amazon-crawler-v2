from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.runtime_source_fingerprint import (
    RuntimeSourceFingerprintError,
    runtime_source_sha256,
)


class RuntimeSourceFingerprintTests(unittest.TestCase):
    def _project(self, root: Path) -> None:
        runtime = root / "src" / "amazon_crawler"
        skill = root / "skills" / "operate-amazon-crawler"
        runtime.mkdir(parents=True)
        skill.mkdir(parents=True)
        (runtime / "runtime.py").write_text("VALUE = 1\n", encoding="utf-8")
        (skill / "SKILL.md").write_text("---\nname: fixture\n---\n", encoding="utf-8")

    def test_hash_changes_when_runtime_source_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            self._project(root)
            first = runtime_source_sha256(root)
            (root / "src" / "amazon_crawler" / "runtime.py").write_text(
                "VALUE = 2\n",
                encoding="utf-8",
            )
            second = runtime_source_sha256(root)

        self.assertRegex(first, r"^[0-9a-f]{64}$")
        self.assertNotEqual(first, second)

    def test_hash_ignores_compiled_cache_but_refuses_source_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            self._project(root)
            baseline = runtime_source_sha256(root)
            cache = root / "src" / "amazon_crawler" / "__pycache__"
            cache.mkdir()
            (cache / "runtime.pyc").write_bytes(b"compiled")
            self.assertEqual(runtime_source_sha256(root), baseline)
            target = root / "target.py"
            target.write_text("SECRET = 1\n", encoding="utf-8")
            (root / "src" / "amazon_crawler" / "linked.py").symlink_to(target)
            with self.assertRaises(RuntimeSourceFingerprintError):
                runtime_source_sha256(root)


if __name__ == "__main__":
    unittest.main()
