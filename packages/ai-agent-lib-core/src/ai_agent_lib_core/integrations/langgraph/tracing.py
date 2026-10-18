"""The agent-tracing entry point at the composition boundary.

One backend ships today: Phoenix. ``agent_tracing`` is the stable name a
service composes with; adding a backend later is a change inside this module,
never in a service.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from ai_agent_lib_core.config.phoenix import PhoenixSettings
from ai_agent_lib_core.contracts import ServiceConfig
from ai_agent_lib_core.integrations.langgraph.phoenix import PhoenixTracing, phoenix_tracing

__all__ = ["agent_tracing"]


@asynccontextmanager
async def agent_tracing(
    application: str,
    config: ServiceConfig,
    *,
    dotenv_path: Path | None = Path(".env"),
    settings: PhoenixSettings | None = None,
) -> AsyncIterator[PhoenixTracing | None]:
    """Yield a running tracing adapter, or None when tracing is not enabled.

    Resolves the Phoenix settings from the process environment and
    ``dotenv_path`` (see ``config.phoenix`` for the variables), starts the
    adapter, and closes it when the context exits. The adapter doubles as the
    model-trace sink: pass the same instance to
    ``ServiceContainer(model_trace=...)`` and to ``build_agent_app(tracing=...)``.

    Args:
        application: The service's name, stamped on every span.
        config: The resolved service configuration.
        dotenv_path: The service's own ``.env``, for the settings layering.
            ``None`` reads the process environment only.
        settings: Already-resolved Phoenix settings. When given, nothing is
            read from the environment or ``dotenv_path``.
    """
    async with phoenix_tracing(
        application, config, dotenv_path=dotenv_path, settings=settings
    ) as tracing:
        yield tracing
