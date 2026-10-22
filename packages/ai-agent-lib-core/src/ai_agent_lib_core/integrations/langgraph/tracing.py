"""Select one optional tracing backend at the service composition boundary."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from ai_agent_lib_core.config.tracing import TracingResolution, resolve_tracing_settings
from ai_agent_lib_core.contracts import ModelTraceSink, ServiceConfig
from ai_agent_lib_core.integrations.langgraph._agent_tracing import tracing_lifetime
from ai_agent_lib_core.integrations.langgraph.langfuse import LangfuseTracing, langfuse_tracing
from ai_agent_lib_core.integrations.langgraph.phoenix import PhoenixTracing, phoenix_tracing

__all__ = ["agent_tracing", "tracing_lifetime"]


@asynccontextmanager
async def agent_tracing(
    application: str,
    config: ServiceConfig,
    *,
    dotenv_path: Path | None = Path(".env"),
    legacy_prefix: str | None = None,
    resolution: TracingResolution | None = None,
    local_sink: ModelTraceSink | None = None,
) -> AsyncIterator[LangfuseTracing | PhoenixTracing | None]:
    """Preserve Langfuse defaults and reject ambiguous dual-export configuration.

    The convenience entry point for a service's ``main``: settings are resolved
    here, at the edge, unless ``resolution`` is given, and the one enabled
    backend's own entry point builds the adapter and owns it through
    :func:`tracing_lifetime`. ``local_sink`` replaces the JSONL model trace
    ``config`` names. Disabled tracing yields ``None`` without loading the
    optional OpenTelemetry SDK.

    Raises:
        ConfigurationError: If both backends are enabled.
    """
    if resolution is None:
        resolution = resolve_tracing_settings(dotenv_path, legacy_prefix=legacy_prefix)
    resolution.validate()
    if resolution.phoenix.settings is not None:
        async with phoenix_tracing(
            application, config, settings=resolution.phoenix.settings, local_sink=local_sink
        ) as phoenix:
            yield phoenix
    elif resolution.langfuse.settings is not None:
        async with langfuse_tracing(
            application, config, settings=resolution.langfuse.settings, local_sink=local_sink
        ) as langfuse:
            yield langfuse
    else:
        yield None
