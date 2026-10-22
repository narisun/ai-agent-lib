"""Optional Langfuse export at the service boundary, independent of business nodes."""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from ai_agent_lib_core.config.langfuse import LangfuseSettings, load_langfuse_settings
from ai_agent_lib_core.contracts import ModelTraceSink, ServiceConfig
from ai_agent_lib_core.integrations.langgraph._agent_tracing import (
    AgentTracing,
    GraphResponse,
    local_model_trace,
    otlp_exporter,
    tracing_lifetime,
)
from ai_agent_lib_core.integrations.langgraph._trace_schema import LangfuseSchema

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
            token = base64.b64encode(
                f"{settings.public_key}:{settings.secret_key}".encode()
            ).decode()
            exporter = otlp_exporter(
                settings.base_url.rstrip("/") + "/api/public/otel/v1/traces",
                headers={"Authorization": "Basic " + token, "x-langfuse-ingestion-version": "4"},
                backend="Langfuse",
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
    local_sink: ModelTraceSink | None = None,
) -> AsyncIterator[LangfuseTracing | None]:
    """Own optional tracing for a service lifetime, flushing after requests drain.

    This is the convenience entry point for a service's ``main``: it loads the
    settings here, at the edge, unless ``settings`` is given, builds the adapter
    and delegates ownership to
    :func:`~ai_agent_lib_core.integrations.langgraph.tracing.tracing_lifetime`.
    Pass the yielded sink as ``ServiceContainer(model_trace=tracing)`` and to
    the HTTP application's response handler. Disabled tracing yields ``None``
    without loading the optional OpenTelemetry SDK or opening a connection.

    Args:
        application: The service name recorded on every span.
        config: The service configuration; its JSONL model trace, if any, is
            preserved as an independent destination unless ``local_sink`` is given.
        dotenv_path: Where the settings are read from when ``settings`` is omitted.
        settings: Already-resolved settings, when the caller loaded them.
        legacy_prefix: A migrating application's old variable prefix.
        local_sink: The local diagnostic sink to use instead of the configured one.
    """
    settings = settings or load_langfuse_settings(dotenv_path, legacy_prefix=legacy_prefix)
    tracing = (
        LangfuseTracing(
            settings,
            application=application,
            local_sink=local_sink if local_sink is not None else local_model_trace(config),
        )
        if settings is not None
        else None
    )
    async with tracing_lifetime(tracing) as owned:
        yield owned
