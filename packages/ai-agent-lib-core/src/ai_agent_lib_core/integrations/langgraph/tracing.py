"""Select one optional tracing backend at the service composition boundary."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from ai_agent_lib_core.config.langfuse import load_langfuse_settings
from ai_agent_lib_core.config.phoenix import load_phoenix_settings
from ai_agent_lib_core.contracts import ConfigurationError, ServiceConfig
from ai_agent_lib_core.integrations.langgraph.langfuse import LangfuseTracing, langfuse_tracing
from ai_agent_lib_core.integrations.langgraph.phoenix import PhoenixTracing, phoenix_tracing

__all__ = ["agent_tracing"]


@asynccontextmanager
async def agent_tracing(
    application: str,
    config: ServiceConfig,
    *,
    dotenv_path: Path | None = Path(".env"),
    legacy_prefix: str | None = None,
) -> AsyncIterator[LangfuseTracing | PhoenixTracing | None]:
    """Preserve Langfuse defaults and reject ambiguous dual-export configuration."""
    phoenix = load_phoenix_settings(dotenv_path)
    if phoenix is not None:
        if load_langfuse_settings(dotenv_path, legacy_prefix=legacy_prefix) is not None:
            raise ConfigurationError("Enable one agent trace destination: Phoenix or Langfuse")
        async with phoenix_tracing(application, config, settings=phoenix) as tracing:
            yield tracing
    else:
        async with langfuse_tracing(
            application, config, dotenv_path=dotenv_path, legacy_prefix=legacy_prefix
        ) as legacy:
            yield legacy
