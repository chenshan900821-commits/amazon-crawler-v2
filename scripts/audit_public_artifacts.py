from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path


RULES = {
    "private_home_path": re.compile(r"/(?:Users|home)/(?!redacted(?:/|\b)|\[)"),
    "unredacted_agent_thread": re.compile(r'"thread_id"\s*:\s*"(?!thread_redacted")'),
    "browser_session_id": re.compile(r"codexSessionId"),
    "bearer_token": re.compile(r"Bearer\s+[A-Za-z0-9._~-]{20,}"),
    "aws_access_key": re.compile(r"AKIA[0-9A-Z]{16}"),
    "cookie_value": re.compile(
        r"(?:session-id|ubid-main|at-main)="
        r"(?!(?:[A-Z][A-Z0-9_]*|<[^>]+>)(?:[;\s\"']|$))"
        r"[^\s;\"']{8,}"
    ),
    "credential_url": re.compile(
        r"(?:redis|rediss|https?)://[^\s/:@]+:[^\s/@]+@[^\s\"']+",
        re.IGNORECASE,
    ),
}

PUBLIC_ROOTS = ("README.md", ".env.example", "docs", "skills")


def candidate_files(root: Path) -> list[Path]:
    output = subprocess.check_output(
        [
            "git",
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "--",
            *PUBLIC_ROOTS,
        ],
        cwd=root,
        text=True,
    )
    return [root / line for line in output.splitlines() if line]


def findings(root: Path) -> list[tuple[str, int, str]]:
    violations: list[tuple[str, int, str]] = []
    for path in candidate_files(root):
        if not path.is_file():
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for line_number, line in enumerate(content.splitlines(), start=1):
            for rule, pattern in RULES.items():
                if pattern.search(line):
                    violations.append((str(path.relative_to(root)), line_number, rule))
    return violations


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fail when public documentation or demo artifacts expose local identity or secret-shaped values."
    )
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    args = parser.parse_args()
    root = args.root.resolve()
    violations = findings(root)
    if violations:
        for path, line, rule in violations:
            print(f"{path}:{line}: {rule}")
        raise SystemExit(1)
    print("public artifact audit passed")


if __name__ == "__main__":
    main()
