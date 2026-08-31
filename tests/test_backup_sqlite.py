from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts.backup_sqlite import backup_database


class BackupSqliteTests(unittest.TestCase):
    def test_backup_is_consistent_and_readable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "crawler.db"
            with sqlite3.connect(source) as database:
                database.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, status TEXT NOT NULL)")
                database.execute("INSERT INTO jobs VALUES ('job-1', 'succeeded')")
                database.commit()

            report = backup_database(source, root / "backups")
            backup = Path(str(report["backup_path"]))
            self.assertTrue(backup.is_file())
            self.assertEqual(report["integrity_check"], "ok")
            self.assertEqual(len(str(report["sha256"])), 64)
            self.assertTrue(backup.with_suffix(".json").is_file())
            with sqlite3.connect(f"file:{backup.as_posix()}?mode=ro", uri=True) as restored:
                self.assertEqual(restored.execute("SELECT * FROM jobs").fetchone(), ("job-1", "succeeded"))


if __name__ == "__main__":
    unittest.main()
