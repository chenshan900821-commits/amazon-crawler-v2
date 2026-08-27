from __future__ import annotations

import hashlib
from pathlib import Path

RUNTIME_SOURCE_ROOTS = (
    Path("src/amazon_crawler"),
    Path("skills/operate-amazon-crawler"),
)
RUNTIME_SOURCE_SUFFIXES = {
    ".css",
    ".html",
    ".js",
    ".json",
    ".md",
    ".py",
    ".svg",
    ".yaml",
    ".yml",
}


class RuntimeSourceFingerprintError(ValueError):
    pass


def runtime_source_files(project_root: Path) -> tuple[Path, ...]:
    root = project_root.resolve()
    files: list[Path] = []
    for relative_root in RUNTIME_SOURCE_ROOTS:
        source_root = root / relative_root
        if source_root.is_symlink() or not source_root.is_dir():
            raise RuntimeSourceFingerprintError(
                f"runtime source root is missing or symlinked: {relative_root}"
            )
        for path in source_root.rglob("*"):
            if "__pycache__" in path.parts or path.suffix not in RUNTIME_SOURCE_SUFFIXES:
                continue
            if path.is_symlink() or not path.is_file():
                raise RuntimeSourceFingerprintError(
                    "runtime source fingerprint refuses symlinked source files"
                )
            files.append(path)
    if not files:
        raise RuntimeSourceFingerprintError("runtime source fingerprint found no files")
    return tuple(sorted(files, key=lambda path: path.relative_to(root).as_posix()))


def runtime_source_sha256(project_root: Path) -> str:
    root = project_root.resolve()
    digest = hashlib.sha256()
    for path in runtime_source_files(root):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        content = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()
