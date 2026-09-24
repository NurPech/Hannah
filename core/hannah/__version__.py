"""Release version of Hannah Core, read from the CI-stamped VERSION file next to
main.py. Written by the `upload:core` job (tarball) and the Dockerfile (container);
absent in a local dev checkout, where VERSION falls back to "dev"."""
from pathlib import Path

_VERSION_FILE = Path(__file__).resolve().parent.parent / "VERSION"


def _read_version() -> str:
    try:
        return _VERSION_FILE.read_text(encoding="utf-8").strip() or "dev"
    except FileNotFoundError:
        return "dev"


VERSION = _read_version()
