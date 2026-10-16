"""The service entry point normalizes interrupts without hiding actual failures."""

import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    ("ending", "code"),
    [
        ("", 0),
        ("raise KeyboardInterrupt", 130),
        ("raise SystemExit(1)", 1),
        ("raise SystemExit(3)", 3),
    ],
)
def test_module_arguments_and_failure_codes_are_preserved(
    tmp_path: Path,
    ending: str,
    code: int,
) -> None:
    (tmp_path / "service.py").write_text(
        "import sys\nassert sys.argv[1:] == ['argument with spaces']\n" + ending
    )
    wrapper = Path(__file__).resolve().parents[2] / "tools/local_service.py"
    result = subprocess.run(  # noqa: S603 - isolated test module
        [sys.executable, str(wrapper), "service", "argument with spaces"],
        cwd=tmp_path,
        capture_output=True,
        check=False,
        timeout=15,
    )
    assert result.returncode == code, result.stderr
