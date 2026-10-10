"""Install this repository into the current Python environment using pip only.

Run from a virtual environment: ``python tools/install_dev.py``. Extra arguments
are passed to pip, for example ``--no-index --find-links=/path/to/wheels``.
"""

from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path


def main() -> int:
    """Resolve all local distributions, extras and development tools together."""
    root = Path(__file__).resolve().parents[1]
    config = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    arguments = [*config["dependency-groups"]["dev"]]
    for group in ("packages", "examples"):
        for project in sorted((root / group).glob("*/pyproject.toml")):
            metadata = tomllib.loads(project.read_text(encoding="utf-8"))["project"]
            extras = ",".join(sorted(metadata.get("optional-dependencies", {})))
            requirement = str(project.parent) + (f"[{extras}]" if extras else "")
            arguments.extend(("--editable", requirement))
    return subprocess.call(  # noqa: S603 - current interpreter and declared requirements
        [sys.executable, "-m", "pip", "install", *arguments, *sys.argv[1:]], cwd=root
    )


if __name__ == "__main__":
    raise SystemExit(main())
