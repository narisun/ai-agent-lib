"""Optional Langfuse export at the service boundary, independent of business nodes."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from ai_agent_lib_core.config.langfuse import LangfuseSettings, load_langfuse_settings
from ai_agent_lib_core.contracts import ConfigurationError, ModelTraceSink, ServiceConfig
from ai_agent_lib_core.integrations.langgraph._agent_tracing import AgentTracing, GraphResponse
from ai_agent_lib_core.integrations.langgraph._trace_schema import LangfuseSchema
from ai_agent_lib_core.observability.model_trace import JsonlModelTraceSink

if TYPE_CHECKING:
    from opentelemetry.sdk.trace.export import SpanExporter

__all__ = ["GraphResponse", "LangfuseTracing", "langfuse_tracing"]


class LangfuseTracing(AgentTracing):
    """Export governed model/tool work to Langfuse using a private OTel provider."""

    settings: LangfuseSettings

    def __init__(
        self,
        settings: LangfuseSettings,
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
                    'Langfuse export needs: python -m pip install "ai-agent-lib-core[otel]"'
                ) from error
            token = base64.b64encode(
                f"{settings.public_key}:{settings.secret_key}".encode()
            ).decode()
            exporter = OTLPSpanExporter(
                endpoint=settings.base_url.rstrip("/") + "/api/public/otel/v1/traces",
                headers={"Authorization": "Basic " + token, "x-langfuse-ingestion-version": "4"},
                timeout=5,
            )
        super().__init__(
            settings,
            application=application,
            exporter=exporter,
            provider_name="langfuse",
            schema=LangfuseSchema(),
            local_sink=local_sink,
            checkpoint_timeout_seconds=checkpoint_timeout_seconds,
        )


@asynccontextmanager
async def langfuse_tracing(
    application: str,
    config: ServiceConfig,
    *,
    dotenv_path: Path | None = Path(".env"),
    settings: LangfuseSettings | None = None,
    legacy_prefix: str | None = None,
) -> AsyncIterator[LangfuseTracing | None]:
    """Own optional tracing for a service lifetime, flushing after requests drain.

    Pass the yielded sink as ``ServiceContainer(model_trace=tracing)`` and to
    the HTTP application's response handler. Disabled tracing yields ``None``
    without loading the optional OpenTelemetry SDK or opening a connection.
    The configured JSONL model trace is preserved as an independent destination.
    """
    settings = settings or load_langfuse_settings(dotenv_path, legacy_prefix=legacy_prefix)
    tracing = (
        LangfuseTracing(
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
