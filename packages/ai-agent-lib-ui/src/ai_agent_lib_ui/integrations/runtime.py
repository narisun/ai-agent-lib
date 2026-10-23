"""One local server composition root; domain services depend on ports."""

import hashlib
from dataclasses import dataclass
from pathlib import Path

import httpx

from ai_agent_lib_core.evaluation.feedback import JsonlFeedbackLog
from ai_agent_lib_ui.adapters.catalog import FileCatalog, LocalAccess
from ai_agent_lib_ui.adapters.http import HttpDispatcher
from ai_agent_lib_ui.adapters.sqlite import SqliteThreads
from ai_agent_lib_ui.application.chat import ChatService
from ai_agent_lib_ui.config.settings import Settings
from ai_agent_lib_ui.config.shell import prepare_shell
from ai_agent_lib_ui.contracts.feedback import FeedbackLog


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
    feedback: FeedbackLog

    @property
    def identity(self) -> str:
        """Opaque identity changes if any configured static identity property changes."""
        return hashlib.sha256(self.settings.identity.model_dump_json().encode()).hexdigest()


def compose(settings: Settings, state: Path) -> Runtime:
    """Construct adapters and inject them; the caller holds the state folder's lock.

    Nothing is stored at module level, so tests compose as many runtimes as they
    need. The server passes the result to ``create_app`` which publishes it for
    the one framework module that cannot receive arguments.
    """
    secret, code = prepare_shell(state, settings)
    access = LocalAccess(settings)
    store = SqliteThreads(state / "conversations.sqlite", access, secret.encode())
    store.recover()
    catalog = FileCatalog(settings, access)
    client = httpx.AsyncClient(trust_env=False, follow_redirects=False)
    chat = ChatService(store, catalog, HttpDispatcher(client, settings.timeout_seconds))
    feedback = JsonlFeedbackLog(state / "feedback.jsonl")
    return Runtime(settings, store, catalog, chat, client, secret, code, feedback)


# Chainlit executes integrations/callbacks.py by file path and calls its decorated
# hooks with framework arguments only, so that module is the single reader of
# this slot. Every other integration receives the Runtime as an argument.
_runtime: Runtime | None = None


def install_runtime(resources: Runtime) -> None:
    """Publish the served Runtime to the Chainlit callback module, once per process."""
    global _runtime
    if _runtime is not None and _runtime is not resources:
        raise RuntimeError("serve one UI per process")
    _runtime = resources


def runtime() -> Runtime:
    """Provide the served Runtime to the Chainlit hooks that cannot be handed one."""
    if _runtime is None:
        raise RuntimeError("UI services have not been composed")
    return _runtime


def reset_runtime() -> None:
    """Forget the published Runtime so a test can serve another composition."""
    global _runtime
    _runtime = None
