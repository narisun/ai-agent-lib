"""One local server composition root; domain services depend on ports."""

import hashlib
from dataclasses import dataclass
from pathlib import Path

import httpx

from ai_agent_lib_ui.adapters.catalog import FileCatalog, LocalAccess
from ai_agent_lib_ui.adapters.http import HttpDispatcher
from ai_agent_lib_ui.adapters.sqlite import SqliteThreads
from ai_agent_lib_ui.application.chat import ChatService
from ai_agent_lib_ui.config.settings import Settings
from ai_agent_lib_ui.config.shell import prepare_shell


@dataclass
class Runtime:
    """Resources owned by one process and closed through the application lifespan."""

    settings: Settings
    store: SqliteThreads
    catalog: FileCatalog
    chat: ChatService
    client: httpx.AsyncClient
    auth_secret: str
    access_code: str

    @property
    def identity(self) -> str:
        """Opaque identity changes if any configured static identity property changes."""
        return hashlib.sha256(self.settings.identity.model_dump_json().encode()).hexdigest()


_runtime: Runtime | None = None


def compose(settings: Settings, state: Path) -> Runtime:
    """Construct adapters and inject them; only this process may own the state lock."""
    global _runtime
    if _runtime is not None:
        raise RuntimeError("serve one UI per process")
    secret, code = prepare_shell(state, settings)
    access = LocalAccess(settings)
    store = SqliteThreads(state / "conversations.sqlite", access, secret.encode())
    store.recover()
    catalog = FileCatalog(settings, access)
    client = httpx.AsyncClient(trust_env=False, follow_redirects=False)
    chat = ChatService(store, catalog, HttpDispatcher(client, settings.timeout_seconds))
    _runtime = Runtime(settings, store, catalog, chat, client, secret, code)
    return _runtime


def runtime() -> Runtime:
    """Provide the composed services to framework callback factories."""
    if _runtime is None:
        raise RuntimeError("UI services have not been composed")
    return _runtime
