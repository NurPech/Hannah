#!/usr/bin/env python3
"""
Validates that CHANGELOG.md's "## **WORK IN PROGRESS**" section has a matching
"### <Section Name>" heading for every component directory touched by a given
commit range — mirrors the test:changelog CI component's dir_keywords check
(.gitlab-ci.yml), so a missing/misnamed section is caught before push instead
of after a CI round-trip.

Usage: check_changelog_sections.py <range>   # e.g. "origin/master..HEAD"
"""

import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CI_FILE = REPO_ROOT / ".gitlab-ci.yml"
CHANGELOG = REPO_ROOT / "CHANGELOG.md"


def parse_dir_keywords() -> dict[str, str]:
    """Extract the dir:Section Name mapping from the test-changelog component's dir_keywords input."""
    text = CI_FILE.read_text(encoding="utf-8")
    m = re.search(r"dir_keywords:\s*>-\s*\n((?:^\s{8}.*\n?)+)", text, re.MULTILINE)
    if not m:
        print("check_changelog_sections: couldn't find dir_keywords in .gitlab-ci.yml, skipping check", file=sys.stderr)
        return {}
    joined = " ".join(line.strip() for line in m.group(1).splitlines())
    mapping = {}
    for pair in joined.split(","):
        pair = pair.strip()
        if not pair or ":" not in pair:
            continue
        d, name = pair.split(":", 1)
        mapping[d.strip()] = name.strip()
    return mapping


def changed_top_level_dirs(rev_range: str) -> set[str]:
    out = subprocess.run(
        ["git", "diff", "--name-only", rev_range],
        cwd=REPO_ROOT, capture_output=True, text=True, check=True,
    ).stdout
    dirs = set()
    for line in out.splitlines():
        if "/" in line:
            dirs.add(line.split("/", 1)[0])
    return dirs


def wip_section_text() -> str:
    text = CHANGELOG.read_text(encoding="utf-8")
    m = re.search(r"^## \*\*WORK IN PROGRESS\*\*\s*\n(.*?)(?=^## |\Z)", text, re.MULTILINE | re.DOTALL)
    return m.group(1) if m else ""


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: check_changelog_sections.py <rev-range>", file=sys.stderr)
        return 2
    rev_range = sys.argv[1]

    mapping = parse_dir_keywords()
    if not mapping:
        return 0

    changed = changed_top_level_dirs(rev_range)
    wip = wip_section_text()

    missing = []
    for d, name in mapping.items():
        if d in changed and not re.search(rf"^### {re.escape(name)}\s*$", wip, re.MULTILINE):
            missing.append((d, name))

    if missing:
        print("check_changelog_sections: CHANGELOG.md's WORK IN PROGRESS section is missing:", file=sys.stderr)
        for d, name in missing:
            print(f"  '### {name}' (changed dir: {d}/)", file=sys.stderr)
        print("See reference_changelog_section_names memory for the valid list. "
              "If this is intentional, push with --no-verify.", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
