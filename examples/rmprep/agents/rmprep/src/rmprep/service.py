"""HTTP entry point: serve the agent until the platform stops it."""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
from collections.abc import AsyncIterator, Sequence
from typing import Any, cast

from ai_agent_lib_core import RequestContext, ServiceConfig, ServiceContainer, ValidationFailed
from ai_agent_lib_core.adapters.audit_jsonl import JsonlAuditOptions
from ai_agent_lib_core.config import load_service_config
from ai_agent_lib_core.contracts import (
    PolicyAction,
    PolicyDenied,
    PolicyRequest,
    PolicyResource,
    Section,
)
from ai_agent_lib_core.contracts.inspection import TurnInspector
from ai_agent_lib_core.integrations.http import (
    AgentService,
    OutcomeJournal,
    ServiceLifecycle,
    SqliteOutcomeJournal,
    build_agent_app,
    serve,
)
from ai_agent_lib_core.integrations.langgraph.inspection import GraphCheckpointPreview
from ai_agent_lib_core.integrations.langgraph.langfuse import LangfuseTracing
from ai_agent_lib_core.integrations.langgraph.phoenix import PhoenixTracing
from ai_agent_lib_core.integrations.langgraph.tracing import agent_tracing
from ai_agent_lib_core.observability import (
    configure_logging,
    report_error,
    shows_details,
    start_telemetry,
)
from ai_agent_lib_core.observability.inspection import LocalTurnInspector

from rmprep.context import ContextBuilder
from rmprep.graph import APPLICATION, build_graph, registered_mcp_tools, response, steps
from rmprep.settings import ENV_FILE, load_context_budget

__all__ = ["SERVICE", "build_app", "main"]


def _question(given: Any) -> str:
    question = given.get("question") if isinstance(given, dict) else None
    if not isinstance(question, str) or not question.strip():
        raise ValidationFailed(
            "the input is not a question",
            expected='an object such as {"question": "Prepare a memo for JONE-0022."}',
            actual="no non-empty 'question' in it",
        )
    return question


async def _build_graph(services: ServiceContainer) -> Any:
    # The service's own default: no context policy. build_app overrides this
    # with one that also carries the request's context_builder.
    return build_graph(services, await registered_mcp_tools(services))


async def _stream(
    graph: Any, services: ServiceContainer, context: RequestContext, given: Any
) -> AsyncIterator[dict[str, Any]]:
    # POST /invoke/stream sends each step as a server-sent event.
    async for step in steps(graph, services, context, _question(given)):
        yield step


def _outcomes(config: ServiceConfig) -> OutcomeJournal | None:
    # Local-only: a deployed service gets no durable journal from this library.
    if config.deployment_env == "local":
        return SqliteOutcomeJournal(ENV_FILE.parent / ".agentlib" / "outcomes.sqlite")
    return None


SERVICE = AgentService(
    application=APPLICATION,
    build_graph=_build_graph,
    respond=response,
    question=_question,
    stream=_stream,
    default_port=8000,
    env_file=ENV_FILE,
    outcomes=_outcomes,
)


async def build_app(
    services: ServiceContainer,
    lifecycle: ServiceLifecycle,
    *,
    context_builder: ContextBuilder | None = None,
    outcomes: OutcomeJournal | None = None,
    inspector: TurnInspector | None = None,
    tracing: LangfuseTracing | PhoenixTracing | None = None,
) -> Any:
    """Build the agent's HTTP application on the shared ``AgentService`` composition.

    The graph is prepared once after the listener starts and before readiness.
    Each request brings its own caller: the entry point turns the bearer token into a
    request context before the graph runs.

    Args:
        services: The container used by request handlers. The lifecycle startup
            callback must start and validate it before graph preparation runs.
        lifecycle: Receives deferred preparation before serving starts.
        context_builder: Optional context policy, shared by normal and streamed requests.
        outcomes: Optional durable outcome journal; see ``SERVICE.outcomes``.
        inspector: Optional diagnostic reader; local services otherwise use their configured
            trace/audit files and graph. Deployed services must inject an authorized reader.
        tracing: Optional library tracing adapter, owned by the entry point.
    """
    # The graph does not exist until the lifecycle's prepare step runs, after
    # this function returns. The inspector's checkpoint reader is built now but
    # only dereferences the graph per request, once it is always present.
    graph_box: list[Any] = []

    async def prepare_graph(services: ServiceContainer) -> Any:
        # Admission stays closed until discovery and graph compilation succeed.
        graph = build_graph(
            services, await registered_mcp_tools(services), context_builder=context_builder
        )
        graph_box.append(graph)
        return graph

    async def authorize_inspection(context: RequestContext) -> None:
        decision = await services.policy.decide(
            PolicyRequest(
                principal=context.principal,
                action=PolicyAction.DIAGNOSTIC_READ,
                resource=PolicyResource(kind="diagnostics", name=APPLICATION),
                application=APPLICATION,
                environment=services.config.deployment_env.value,
            )
        )
        if not decision.allow:
            raise PolicyDenied("diagnostic access denied", reason_code="diagnostic_denied")

    if inspector is None and services.config.deployment_env == "local":
        trace = services.config.model_trace_path
        audit = services.config.section(Section.AUDIT)
        inspector = LocalTurnInspector(
            authorize=authorize_inspection,
            model_paths=(trace,) if trace else (),
            audit_paths=(audit.parse_options(JsonlAuditOptions).path,)
            if audit.provider == "jsonl"
            else (),
            checkpoints=GraphCheckpointPreview(
                lambda: graph_box[-1] if graph_box else None, services.invocation
            ),
        )

    service = dataclasses.replace(
        SERVICE,
        build_graph=prepare_graph,
        app_kwargs={**dict(SERVICE.app_kwargs), "outcomes": outcomes, "inspector": inspector},
    )
    # build_agent_app's tracing parameter predates Phoenix support; both backends
    # share the same run(graph, services, context, question, respond=...) shape.
    return await build_agent_app(
        service, services, lifecycle, tracing=cast("LangfuseTracing | None", tracing)
    )


async def _serve(config: ServiceConfig, host: str, port: int, contexts: ContextBuilder) -> None:
    async with agent_tracing(
        APPLICATION, config, dotenv_path=ENV_FILE, legacy_prefix="RMPREP"
    ) as tracing:
        services = ServiceContainer(config, model_trace=tracing)

        async def start() -> None:
            await services.start()
            await services.validate()

        lifecycle = ServiceLifecycle(start)
        try:
            # The listener is available during adapter startup and graph preparation.
            await serve(
                await build_app(
                    services,
                    lifecycle,
                    context_builder=contexts,
                    tracing=tracing,
                    outcomes=_outcomes(config),
                ),
                lifecycle,
                host=host,
                port=port,
            )
        finally:
            await services.aclose()


def main(argv: Sequence[str] | None = None) -> int:
    """Serve until stopped. SIGTERM lets requests in flight finish first."""
    parser = argparse.ArgumentParser(prog="rmprep-serve", description=__doc__)
    parser.add_argument("--host", default="127.0.0.1", help="address to listen on")
    parser.add_argument("--port", type=int, default=SERVICE.default_port, help="port to listen on")
    arguments = parser.parse_args(argv)
    config: ServiceConfig | None = None
    try:
        config = load_service_config(ENV_FILE)
        contexts = ContextBuilder(load_context_budget(ENV_FILE))
        # One line of JSON per event on standard output. On a developer's
        # machine a line about an error also says what the error said.
        configure_logging(APPLICATION, details=shows_details(config))
        # Traces and metrics go to the collector when the settings turn them on.
        stop_telemetry = start_telemetry(APPLICATION, config)
        try:
            asyncio.run(_serve(config, arguments.host, arguments.port, contexts))
        finally:
            stop_telemetry()  # the last spans and metrics are flushed
    except Exception as error:
        # What went wrong, what was expected, and the line of this code to look at.
        return report_error("rmprep-serve", error, details=shows_details(config))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
