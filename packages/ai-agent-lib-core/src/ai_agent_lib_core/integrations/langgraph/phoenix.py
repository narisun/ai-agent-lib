"""Optional Phoenix OTLP tracing with an independent exporter and local JSONL sink."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from ai_agent_lib_core.config.phoenix import PhoenixSettings, load_phoenix_settings
from ai_agent_lib_core.contracts import ModelTraceSink, ServiceConfig
from ai_agent_lib_core.integrations.langgraph._agent_tracing import (
    AgentTracing,
    local_model_trace,
    otlp_exporter,
    tracing_lifetime,
)
from ai_agent_lib_core.integrations.langgraph._openinference import PhoenixSchema

__all__ = ["PhoenixTracing", "phoenix_tracing"]

if TYPE_CHECKING:
    from opentelemetry.sdk.trace.export import SpanExporter


class PhoenixTracing(AgentTracing):
    """Use OpenInference attributes without globally instrumenting agent libraries."""

    settings: PhoenixSettings

    def __init__(
        self,
        settings: PhoenixSettings,
        *,
        application: str,
        exporter: SpanExporter | None = None,
        local_sink: ModelTraceSink | None = None,
        checkpoint_timeout_seconds: float = 1.0,
    ) -> None:
        if exporter is None:
            exporter = otlp_exporter(
                settings.base_url.rstrip("/") + "/v1/traces",
                headers={"Authorization": "Bearer " + settings.api_key} if settings.api_key else {},
                backend="Phoenix",
            )
        super().__init__(
            settings,
            application=application,
            exporter=exporter,
            provider_name="phoenix",
            schema=PhoenixSchema(),
            resource_attributes={"openinference.project.name": settings.project_name},
            local_sink=local_sink,
            checkpoint_timeout_seconds=checkpoint_timeout_seconds,
        )


@asynccontextmanager
async def phoenix_tracing(
    application: str,
    config: ServiceConfig,
    *,
    dotenv_path: Path | None = Path(".env"),
    settings: PhoenixSettings | None = None,
    local_sink: ModelTraceSink | None = None,
) -> AsyncIterator[PhoenixTracing | None]:
    """Own the optional exporter until service shutdown, preserving local diagnostics.

    The convenience entry point for a service's ``main``: settings are loaded
    here, at the edge, unless ``settings`` is given, and ownership is delegated to
    :func:`~ai_agent_lib_core.integrations.langgraph.tracing.tracing_lifetime`.
    ``local_sink`` replaces the JSONL model trace ``config`` names.
    """
    settings = settings or load_phoenix_settings(dotenv_path)
    tracing = (
        PhoenixTracing(
            settings,
            application=application,
            local_sink=local_sink if local_sink is not None else local_model_trace(config),
        )
        if settings is not None
        else None
    )
    async with tracing_lifetime(tracing) as owned:
        yield owned
