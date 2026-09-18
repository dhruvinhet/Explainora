"""Reject common credential formats in files tracked by git."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PATTERNS = {
    "Supabase secret key": re.compile(
        r"sb_secret_(?!replace|your)[A-Za-z0-9_-]{12,}", re.IGNORECASE
    ),
    "OpenAI key": re.compile(r"sk-(?!replace|your)[A-Za-z0-9_-]{20,}", re.IGNORECASE),
    "GitHub token": re.compile(r"gh[opurs]_[A-Za-z0-9]{30,}"),
    "Private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "Database URL with password": re.compile(r"postgres(?:ql)?://[^\s:@]+:[^\s@]+@", re.IGNORECASE),
}


def main() -> int:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    findings: list[str] = []
    for raw in result.stdout.split(b"\0"):
        if not raw:
            continue
        relative = raw.decode("utf-8", errors="surrogateescape")
        path = ROOT / relative
        try:
            if path.stat().st_size > 5_000_000:
                continue
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        for label, pattern in PATTERNS.items():
            if pattern.search(text):
                findings.append(f"{relative}: possible {label}")
    if findings:
        print("\n".join(findings))
        return 1
    print("No common secret patterns found in tracked files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
