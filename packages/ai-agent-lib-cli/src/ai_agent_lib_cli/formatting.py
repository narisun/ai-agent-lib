"""Formatting generated Python, so that it is formatted whatever the names are.

A template cannot be laid out for every name: a longer service name moves a
line break. So the rendered files are put through the formatter the generated
workspace itself uses, with the same settings, before they are written.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path, PurePosixPath

from ai_agent_lib_cli.errors import CliError
from ai_agent_lib_core.contracts import describe

__all__ = ["LINE_LENGTH", "TARGET_VERSION", "Formatter", "format_python"]

LINE_LENGTH = 100
TARGET_VERSION = "py311"

Formatter = Callable[[Mapping[PurePosixPath, str], Sequence[str]], dict[PurePosixPath, str]]
"""Given files by path and the workspace's own package names, returns the files formatted."""

_TIMEOUT_SECONDS = 120


def _settings(first_party: Sequence[str]) -> str:
    return (
        f"line-length = {LINE_LENGTH}\n"
        f'target-version = "{TARGET_VERSION}"\n'
        "[lint]\n"
        'select = ["I"]\n'
        "[lint.isort]\n"
        f"known-first-party = {json.dumps(sorted(first_party))}\n"
    )


def _ruff(folder: Path, *arguments: str) -> None:
    command = [sys.executable, "-m", "ruff", *arguments, "--quiet", "--no-cache", "."]
    try:
        done = subprocess.run(  # noqa: S603 - this interpreter and fixed arguments
            command,
            cwd=folder,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CliError(f"the generated code could not be formatted ({describe(exc)})") from exc
    if done.returncode != 0:
        last = ((done.stderr or done.stdout).strip().splitlines() or ["no output"])[-1]
        raise CliError(f"the generated code could not be formatted: {last}")


def format_python(
    files: Mapping[PurePosixPath, str], first_party: Sequence[str]
) -> dict[PurePosixPath, str]:
    """Return ``files`` with every Python file's imports sorted and its code formatted.

    Args:
        files: The rendered files, by path relative to the workspace.
        first_party: The Python packages of the workspace's own services.

    Raises:
        CliError: If the formatter cannot be run or rejects a file.
    """
    python = {path: text for path, text in files.items() if path.suffix == ".py"}
    if not python:
        return dict(files)
    with tempfile.TemporaryDirectory(prefix="agentlib-") as scratch:
        folder = Path(scratch)
        (folder / "ruff.toml").write_text(_settings(first_party), encoding="utf-8")
        for path, text in python.items():
            target = folder.joinpath(*path.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8", newline="\n")
        _ruff(folder, "check", "--fix")
        _ruff(folder, "format")
        formatted = {
            path: folder.joinpath(*path.parts).read_text(encoding="utf-8") for path in python
        }
    return {**files, **formatted}
