"""Profile selection must not run an async callback on an already running loop."""

import os
import subprocess
from pathlib import Path

import pytest
from python_runtime import python_command

pytestmark = pytest.mark.slow


def test_profile_config_is_shared_without_creating_a_coroutine(tmp_path: Path) -> None:
    # Chainlit initializes global configuration on import. Isolate that state
    # and upstream import deprecations from the framework-independent UI tests.
    code = """
import asyncio
from unittest.mock import AsyncMock
from chainlit.config import config
from chainlit.session import WebsocketSession
from agentcraft2_ui.integrations.chainlit_compat import use_shared_profile_config
async def check():
    callback = AsyncMock()
    config.code.set_chat_profiles = callback
    use_shared_profile_config()
    session = object.__new__(WebsocketSession)
    session.chat_profile = 'rmprep'
    session.user = None
    session.language = 'en-US'
    assert session.get_config() is config
    callback.assert_not_called()
    session.config = config
    assert session.get_config() is config
asyncio.run(check())
"""
    result = subprocess.run(  # noqa: S603 - installed shell, isolated temporary configuration
        python_command("-c", code),
        cwd=tmp_path,
        env={**os.environ, "CHAINLIT_APP_ROOT": str(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "was never awaited" not in result.stderr
