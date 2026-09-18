"""Fast repository checks for ordered, reviewable Supabase migrations."""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "supabase" / "migrations"
NAME = re.compile(r"^(\d{14})_[a-z0-9_]+\.sql$")


def main() -> int:
    errors: list[str] = []
    versions: list[str] = []
    files = sorted(MIGRATIONS.glob("*.sql"))
    if not files:
        errors.append("No Supabase migrations found")
    for path in files:
        match = NAME.fullmatch(path.name)
        if not match:
            errors.append(f"Invalid migration filename: {path.name}")
            continue
        version = match.group(1)
        if version in versions:
            errors.append(f"Duplicate migration version: {version}")
        versions.append(version)
        sql = path.read_text(encoding="utf-8")
        if not sql.strip():
            errors.append(f"Empty migration: {path.name}")
        if re.search(r"security\s+definer", sql, re.IGNORECASE):
            # A fixed search path prevents object-shadowing attacks in
            # privileged functions. Existing migrations follow this rule.
            if not re.search(r"set\s+search_path\s*=", sql, re.IGNORECASE):
                errors.append(f"SECURITY DEFINER without fixed search_path: {path.name}")
    if versions != sorted(versions):
        errors.append("Migration versions are not ordered")
    if errors:
        print("\n".join(errors))
        return 1
    print(f"Validated {len(files)} ordered migration files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
