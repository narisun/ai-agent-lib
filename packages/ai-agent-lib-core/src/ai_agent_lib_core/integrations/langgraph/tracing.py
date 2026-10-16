"""Select one optional tracing backend at the service composition boundary."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from ai_agent_lib_core.config.tracing import resolve_tracing_settings
from ai_agent_lib_core.contracts import ServiceConfig
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
    resolved = resolve_tracing_settings(dotenv_path, legacy_prefix=legacy_prefix)
    phoenix = resolved.phoenix.settings
    if phoenix is not None:
        async with phoenix_tracing(application, config, settings=phoenix) as tracing:
            yield tracing
    elif resolved.langfuse.settings is not None:
        async with langfuse_tracing(
            application,
            config,
            dotenv_path=dotenv_path,
            legacy_prefix=legacy_prefix,
            settings=resolved.langfuse.settings,
        ) as legacy:
            yield legacy
    else:
        yield None
