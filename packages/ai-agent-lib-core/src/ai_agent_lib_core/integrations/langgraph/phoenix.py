"""Optional Phoenix OTLP tracing with an independent exporter and local JSONL sink."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from ai_agent_lib_core.config.phoenix import PhoenixSettings, load_phoenix_settings
from ai_agent_lib_core.contracts import ConfigurationError, ModelTraceSink, ServiceConfig
from ai_agent_lib_core.integrations.langgraph._agent_tracing import AgentTracing
from ai_agent_lib_core.integrations.langgraph._openinference import PhoenixSchema
from ai_agent_lib_core.observability.model_trace import JsonlModelTraceSink

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
            try:
                from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            except ImportError as error:
                raise ConfigurationError(
                    'Phoenix export needs: python -m pip install "ai-agent-lib-core[otel]"'
                ) from error
            exporter = OTLPSpanExporter(
                endpoint=settings.base_url.rstrip("/") + "/v1/traces",
                headers={"Authorization": "Bearer " + settings.api_key} if settings.api_key else {},
                timeout=5,
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
) -> AsyncIterator[PhoenixTracing | None]:
    """Own the optional exporter until service shutdown, preserving local diagnostics."""
    settings = settings or load_phoenix_settings(dotenv_path)
    tracing = (
        PhoenixTracing(
            settings,
            application=application,
            local_sink=JsonlModelTraceSink(config.model_trace_path)
            if config.model_trace_path
            else None,
        )
        if settings is not None
        else None
    )
    try:
        yield tracing
    finally:
        if tracing is not None:
            await asyncio.to_thread(tracing.close)
