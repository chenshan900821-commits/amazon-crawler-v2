from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def backup_database(source: Path, output_dir: Path) -> dict[str, str | int]:
    source = source.expanduser().resolve(strict=True)
    if not source.is_file():
        raise ValueError("source database must be a file")
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    destination = output_dir / f"crawler-{timestamp}.sqlite3"
    if destination.exists():
        raise FileExistsError(destination)

    source_uri = f"file:{source.as_posix()}?mode=ro"
    with sqlite3.connect(source_uri, uri=True) as source_db:
        with sqlite3.connect(destination) as backup_db:
            source_db.backup(backup_db)
            integrity = backup_db.execute("PRAGMA integrity_check").fetchone()
            if not integrity or integrity[0] != "ok":
                raise RuntimeError("backup integrity check failed")

    report: dict[str, str | int] = {
        "created_at": datetime.now(UTC).isoformat(),
        "backup_path": str(destination),
        "bytes": destination.stat().st_size,
        "sha256": _sha256(destination),
        "integrity_check": "ok",
    }
    manifest = destination.with_suffix(".json")
    manifest.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Create and verify an online SQLite backup.")
    parser.add_argument(
        "--db",
        type=Path,
        default=Path(os.getenv("CRAWLER_DB_PATH", ".data/crawler.db")),
    )
    parser.add_argument("--output-dir", type=Path, default=Path(".data/backups"))
    args = parser.parse_args()
    print(json.dumps(backup_database(args.db, args.output_dir), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
