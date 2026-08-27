#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from scripts.compile_controlled_evidence import ControlledEvidenceError
from scripts.verify_parity_manifest import _validate_cookie_production_evidence


def compile_cookie_production_evidence(
    source_path: Path,
    *,
    project_root: Path,
    output_path: Path,
) -> dict[str, Any]:
    project_root = project_root.resolve()
    unresolved_source = source_path
    source_path = source_path.resolve()
    output_path = output_path.resolve()
    if unresolved_source.is_symlink() or source_path.is_relative_to(project_root):
        raise ControlledEvidenceError(
            "raw Cookie production evidence must be a non-symlink outside the project"
        )
    if output_path != project_root / "evidence" / "controlled" / "cookie-production.json":
        raise ControlledEvidenceError(
            "compiled Cookie production evidence must use the canonical project path"
        )
    if output_path.is_symlink():
        raise ControlledEvidenceError(
            "refusing to replace a symlinked Cookie production receipt"
        )
    errors = _validate_cookie_production_evidence(source_path)
    if errors:
        raise ControlledEvidenceError(
            "raw Cookie production evidence failed validation: " + "; ".join(errors)
        )
    payload = json.loads(source_path.read_text(encoding="utf-8"))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".cookie-production.",
        suffix=".tmp",
        dir=output_path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(payload, output, ensure_ascii=False, indent=2, sort_keys=True)
            output.write("\n")
        os.replace(temporary_path, output_path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()
    return {
        "ok": True,
        "schema_version": payload["schema_version"],
        "operation_count": len(payload["operations"]),
        "output": str(output_path.relative_to(project_root)),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Validate a project-external redacted Cookie production receipt and "
            "atomically compile it to the canonical controlled evidence path."
        )
    )
    project_root = Path(__file__).resolve().parents[1]
    parser.add_argument("source", type=Path)
    parser.add_argument(
        "--output",
        type=Path,
        default=project_root / "evidence" / "controlled" / "cookie-production.json",
    )
    args = parser.parse_args()
    try:
        result = compile_cookie_production_evidence(
            args.source,
            project_root=project_root,
            output_path=args.output,
        )
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        ControlledEvidenceError,
        ValueError,
    ) as exc:
        message = (
            str(exc)
            if isinstance(exc, ControlledEvidenceError)
            else "Cookie production evidence could not be compiled"
        )
        print(
            json.dumps(
                {"ok": False, "error": {"type": type(exc).__name__, "message": message}},
                ensure_ascii=False,
            )
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
