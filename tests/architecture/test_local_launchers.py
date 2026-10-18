"""Both launcher shells delegate to the same tools, so neither drifts."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PAIRS = [
    ("setup-local.bat", "setup-local.sh"),
    ("start-local.bat", "start-local.sh"),
    ("stop-local.bat", "stop-local.sh"),
    ("start-phoenix.bat", "start-phoenix.sh"),
]


def _tool(text: str) -> str:
    found = re.search(r"tools[\\/](\w+)\.py", text)
    assert found, f"no tools/<module>.py call in: {text!r}"
    return found.group(1)


@pytest.mark.parametrize(("bat", "sh"), PAIRS)
def test_each_shell_pair_calls_the_same_tool(bat: str, sh: str) -> None:
    bat_text = (ROOT / bat).read_text(encoding="utf-8")
    sh_text = (ROOT / sh).read_text(encoding="utf-8")
    assert _tool(bat_text) == _tool(sh_text)
    assert '"$@"' in sh_text  # arguments pass through


@pytest.mark.parametrize(("bat", "sh"), PAIRS)
def test_the_posix_wrapper_is_executable(bat: str, sh: str) -> None:
    assert (ROOT / sh).stat().st_mode & 0o111, f"{sh} is not executable"
