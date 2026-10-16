"""Install this repository into the current Python environment using pip only.

Run with your selected Python: ``python tools/install_dev.py``. Extra arguments
are passed to pip, for example ``--no-index --find-links=/path/to/wheels``.
"""

from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path


def editable_requirements(project: Path) -> list[str]:
    """Find distributions inside a project or a generated service workspace.

    A CLI workspace root groups services and is not itself installable. Follow
    its declared members so pip installs the agent and MCP distributions.
    """
    config = tomllib.loads(project.read_text(encoding="utf-8"))
    # UI dependencies have their own package folder and checks. In particular the
    # selected browser framework and the agent MCP SDK need different versions.
    if config.get("tool", {}).get("ai-agent-lib", {}).get("development-environment") == "ui":
        return []
    workspace = config.get("tool", {}).get("uv", {})
    if workspace.get("package") is False:
        return [
            requirement
            for pattern in workspace.get("workspace", {}).get("members", [])
            for member in sorted(project.parent.glob(f"{pattern}/pyproject.toml"))
            for requirement in editable_requirements(member)
        ]
    metadata = config["project"]
    extras = ",".join(sorted(metadata.get("optional-dependencies", {})))
    return [str(project.parent) + (f"[{extras}]" if extras else "")]


def main() -> int:
    """Resolve all local distributions, extras and development tools together."""
    root = Path(__file__).resolve().parents[1]
    config = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    arguments = [*config["dependency-groups"]["dev"]]
    for group in ("packages", "examples"):
        for project in sorted((root / group).glob("*/pyproject.toml")):
            for requirement in editable_requirements(project):
                arguments.extend(("--editable", requirement))
    return subprocess.call(  # noqa: S603 - current interpreter and declared requirements
        [sys.executable, "-m", "pip", "install", *arguments, *sys.argv[1:]], cwd=root
    )


if __name__ == "__main__":
    raise SystemExit(main())
