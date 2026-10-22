"""Shared fixtures: one composed Runtime per test, isolated from the process."""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from local_runtime import settings_at

from ai_agent_lib_ui.integrations.runtime import Runtime, compose, reset_runtime

SHELL_VARIABLES = (
    "CHAINLIT_APP_ROOT",
    "CHAINLIT_ENV_FILE",
    "CHAINLIT_AUTH_SECRET",
    "CHAINLIT_AUTH_COOKIE_NAME",
    "CHAINLIT_COOKIE_SAMESITE",
)


@pytest.fixture
async def runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Runtime]:
    """Compose real adapters over a temporary state folder; restore the environment after."""
    for name in SHELL_VARIABLES:
        # prepare_shell exports these; recording them first makes monkeypatch restore them.
        monkeypatch.setenv(name, "")
    resources = compose(settings_at(tmp_path), tmp_path / "state")
    try:
        yield resources
    finally:
        await resources.chat.close()
        await resources.client.aclose()
        reset_runtime()
