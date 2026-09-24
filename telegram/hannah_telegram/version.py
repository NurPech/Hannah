"""Release version of hannah-telegram, read from the CI-stamped VERSION file next to
main.py. Written by the `upload:telegram` job (tarball) and the Dockerfile
(container); absent in a local dev checkout, where get_version() falls back to "dev"."""
from __future__ import annotations

from pathlib import Path

_VERSION_FILE = Path(__file__).resolve().parent.parent / "VERSION"


def get_version() -> str:
    try:
        return _VERSION_FILE.read_text(encoding="utf-8").strip() or "dev"
    except FileNotFoundError:
        return "dev"
