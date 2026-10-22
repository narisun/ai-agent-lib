"""Shared fixtures: one composed Runtime per test, isolated from the process."""

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from browser_protocol import LocalServer
from chat_support import ScriptedAgent, chat_at
from local_runtime import settings_at

from ai_agent_lib_ui.adapters.sqlite import SqliteThreads
from ai_agent_lib_ui.application.chat import ChatService
from ai_agent_lib_ui.config.settings import Settings
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


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Pin the two-agent loopback registry written into this test's folder."""
    return settings_at(tmp_path)


@pytest.fixture
async def chat(
    settings: Settings, tmp_path: Path
) -> AsyncIterator[tuple[ChatService, ScriptedAgent, SqliteThreads]]:
    """Serve a ChatService over a scripted agent; closed and drained after the test."""
    async with chat_at(settings, tmp_path) as resources:
        yield resources


@pytest.fixture(scope="session")
def ui_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[LocalServer]:
    """Start one real UI server process for the whole session; check its log at the end."""
    value = LocalServer(tmp_path_factory.mktemp("local-ui"))
    try:
        value.start()
        yield value
    finally:
        value.close()
        assert "was never awaited" not in (value.path / "server.log").read_text(encoding="utf-8")


@pytest.fixture
def server(ui_server: LocalServer) -> LocalServer:
    """Hand each test the shared server with the fake agent's steering reset to defaults."""
    ui_server.reset()
    return ui_server
