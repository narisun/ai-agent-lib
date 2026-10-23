"""A Windows process-group stop must return control to the service's resource cleanup."""

from __future__ import annotations

import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(sys.platform != "win32", reason="Windows console process groups"),
]


def test_ctrl_break_returns_from_serve_and_restores_handler(tmp_path: Path) -> None:
    script = tmp_path / "service.py"
    script.write_text(
        """
import asyncio
import signal
from pathlib import Path
from starlette.applications import Starlette
from ai_agent_lib_core.integrations.http import ServiceLifecycle, serve

async def main():
    previous = signal.getsignal(signal.SIGBREAK)
    async def ready():
        Path("ready").touch()
    try:
        await serve(Starlette(), ServiceLifecycle(ready), port=0, drain_seconds=30)
    finally:
        Path("closed").write_text("resources released", encoding="utf-8")
    assert signal.getsignal(signal.SIGBREAK) == previous

asyncio.run(main())
""",
        encoding="utf-8",
    )
    log = tmp_path / "service.log"
    with log.open("wb") as output:
        process = subprocess.Popen(  # noqa: S603 - local test fixture
            [sys.executable, str(script)],
            cwd=tmp_path,
            stdout=output,
            stderr=subprocess.STDOUT,
            # Windows only: the module mark skips elsewhere, and mypy on Linux has no such name.
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,  # type: ignore[attr-defined,unused-ignore]
        )
    try:
        deadline = time.monotonic() + 15
        while not (tmp_path / "ready").exists():
            assert process.poll() is None, log.read_text(encoding="utf-8")
            assert time.monotonic() < deadline, "service did not reach startup"
            time.sleep(0.05)
        process.send_signal(signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined,unused-ignore]
        # A keyboard stop skips the 30-second drain but still exits through finally.
        assert process.wait(timeout=10) == 0, log.read_text(encoding="utf-8")
        assert (tmp_path / "closed").read_text(encoding="utf-8") == "resources released"
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
