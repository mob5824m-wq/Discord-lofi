#!/usr/bin/env python3
"""
Bump VERSION with +1 (patch by default). Used by CI auto-release on merge to main.

- Reads VERSION (e.g. 1.0.0)
- Increments patch (1.0.0 -> 1.0.1) unless --minor / --major
- Writes back to VERSION
- Prints new version to stdout and to GITHUB_OUTPUT if present

Usage:
  python scripts/bump_version.py           # patch bump
  python scripts/bump_version.py --minor   # 1.0.0 -> 1.1.0
  python scripts/bump_version.py --major   # 1.0.0 -> 2.0.0
  python scripts/bump_version.py --dry-run # print without writing
  cat VERSION | python scripts/bump_version.py --patch
"""

from __future__ import annotations
import argparse
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION_FILE = ROOT / "VERSION"
SEMVER_RE = re.compile(r"^\s*v?(\d+)\.(\d+)\.(\d+)(?:[-+].*)?\s*$")

def parse(v: str) -> tuple[int, int, int]:
    m = SEMVER_RE.match(v)
    if not m:
        raise ValueError(f"VERSION '{v}' is not semver X.Y.Z")
    return int(m.group(1)), int(m.group(2)), int(m.group(3))

def bump(v: str, part: str) -> str:
    major, minor, patch = parse(v)
    if part == "major":
        major += 1
        minor = 0
        patch = 0
    elif part == "minor":
        minor += 1
        patch = 0
    else:  # patch
        patch += 1
    return f"{major}.{minor}.{patch}"

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Bump VERSION +1")
    p.add_argument("--major", action="store_const", const="major", dest="part", help="bump major")
    p.add_argument("--minor", action="store_const", const="minor", dest="part", help="bump minor")
    p.add_argument("--patch", action="store_const", const="patch", dest="part", help="bump patch (default)")
    p.add_argument("--dry-run", action="store_true", help="print new version without writing")
    p.add_argument("--file", default=str(VERSION_FILE), help="path to VERSION file")
    p.add_argument("version", nargs="?", help="explicit version to bump (default: read file)")
    args = p.parse_args(argv)
    part = args.part or "patch"
    src = Path(args.file)
    current = args.version if args.version is not None else src.read_text(encoding="utf-8")
    current = current.strip()
    if not current:
        print("VERSION file is empty", file=sys.stderr)
        return 2
    try:
        new = bump(current, part)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2
    if args.dry_run:
        print(new)
        return 0
    src.write_text(new + "\n", encoding="utf-8")
    print(new)
    # GitHub Actions output
    gh = os.environ.get("GITHUB_OUTPUT")
    if gh:
        with open(gh, "a", encoding="utf-8") as f:
            f.write(f"ver={new}\n")
            f.write(f"old={current}\n")
            f.write(f"new={new}\n")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
