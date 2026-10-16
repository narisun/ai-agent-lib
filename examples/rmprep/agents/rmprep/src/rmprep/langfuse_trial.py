"""Compatibility facade for callers of the original rmprep trial adapter."""

from typing import Any

from ai_agent_lib_core import RequestContext, ServiceContainer
from ai_agent_lib_core.config.langfuse import LangfuseSettings
from ai_agent_lib_core.contracts import ModelTraceSink
from ai_agent_lib_core.integrations.langgraph.langfuse import GraphResponse, LangfuseTracing
from opentelemetry.sdk.trace.export import SpanExporter

from rmprep.graph import APPLICATION, response


class LangfuseTrial(LangfuseTracing):
    """Delegate the original constructor and response method to library tracing."""

    def __init__(
        self,
        settings: LangfuseSettings,
        *,
        exporter: SpanExporter | None = None,
        local_sink: ModelTraceSink | None = None,
    ) -> None:
        super().__init__(
            settings, application=APPLICATION, exporter=exporter, local_sink=local_sink
        )

    async def run(
        self,
        graph: Any,
        services: ServiceContainer,
        context: RequestContext,
        given: Any = None,
        *,
        respond: GraphResponse = response,
        question: str | None = None,
    ) -> dict[str, Any]:
        """Keep the original response handler and ``question=`` keyword working."""
        if question is not None:
            if given is not None:
                raise TypeError("supply either given or question")
            given = question
        return await super().run(graph, services, context, given, respond=respond)
