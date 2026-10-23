"""Exercise memo selection races when an existing Node runtime is available."""

import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.slow


def test_switch_back_to_rendered_memo_while_another_version_is_loading() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Optional browser-asset check requires an existing Node runtime")
    result = subprocess.run(  # noqa: S603 - test-owned JavaScript, no external requests
        [node, str(Path(__file__).with_name("memo_panel.cjs"))],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
