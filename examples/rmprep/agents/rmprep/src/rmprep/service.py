"""HTTP entry point: serve the agent until the platform stops it."""

from __future__ import annotations

import dataclasses
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from typing import Any

from agentcraft2_core import ConfigurationError, RequestContext, ServiceConfig, ServiceContainer
from agentcraft2_core.adapters.audit_jsonl import JsonlAuditOptions
from agentcraft2_core.contracts import (
    PolicyAction,
    PolicyDenied,
    PolicyRequest,
    PolicyResource,
    Section,
)
from agentcraft2_core.contracts.inspection import TurnInspector
from agentcraft2_core.integrations.http import (
    AgentService,
    OutcomeJournal,
    ServiceLifecycle,
    SqliteOutcomeJournal,
    build_agent_app,
    run_agent,
)
from agentcraft2_core.integrations.langgraph import AgentTracing
from agentcraft2_core.integrations.langgraph.inspection import GraphCheckpointPreview
from agentcraft2_core.observability import report_error
from agentcraft2_core.observability.inspection import LocalTurnInspector

from rmprep.context import ContextBuilder
from rmprep.graph import APPLICATION, build_graph, registered_mcp_tools, response, steps
from rmprep.settings import ENV_FILE, load_context_budget

__all__ = ["SERVICE", "build_app", "main"]


def _build_graph_with(
    contexts: ContextBuilder | None,
) -> Callable[[ServiceContainer], Awaitable[Any]]:
    """The graph builder for one context policy; ``None`` is no policy."""

    async def build(services: ServiceContainer) -> Any:
        # Admission stays closed until discovery and graph compilation succeed.
        return build_graph(services, await registered_mcp_tools(services), context_builder=contexts)

    return build


async def _build_graph(services: ServiceContainer) -> Any:
    # SERVICE on its own reads the context settings here, when the graph is built, so
    # importing this module reads none. That also means a bare ``run_agent(SERVICE)``
    # surfaces a malformed budget during graph preparation, after the listener has
    # started; only ``main`` is fail-fast. It loads the settings first, so a malformed
    # value fails before anything starts, and replaces this builder with the loaded budget.
    return await _build_graph_with(ContextBuilder(load_context_budget(ENV_FILE)))(services)


async def _stream(
    graph: Any, services: ServiceContainer, context: RequestContext, given: Any
) -> AsyncIterator[dict[str, Any]]:
    # POST /invoke/stream sends each step as a server-sent event.
    async for step in steps(graph, services, context, SERVICE.question(given)):
        yield step


def _outcomes(config: ServiceConfig) -> OutcomeJournal | None:
    # Local-only: a deployed service gets no durable journal from this library.
    if config.deployment_env == "local":
        return SqliteOutcomeJournal(ENV_FILE.parent / ".agentcraft2" / "outcomes.sqlite")
    return None


def _local_inspector(services: ServiceContainer, graph: Callable[[], Any]) -> TurnInspector | None:
    """The diagnostic reader of a local service; a deployed one must inject its own."""
    if services.config.deployment_env != "local":
        return None

    async def authorize(context: RequestContext) -> None:
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

    trace = services.config.model_trace_path
    audit = services.config.section(Section.AUDIT)
    return LocalTurnInspector(
        authorize=authorize,
        model_paths=(trace,) if trace else (),
        audit_paths=(audit.parse_options(JsonlAuditOptions).path,)
        if audit.provider == "jsonl"
        else (),
        # The graph is None until preparation finishes, which readiness waits for.
        checkpoints=GraphCheckpointPreview(graph, services.invocation),
    )


def _extras(services: ServiceContainer, graph: Callable[[], Any]) -> dict[str, Any]:
    # What only exists once the service is composed: the live container and the graph.
    inspector = _local_inspector(services, graph)
    return {} if inspector is None else {"inspector": inspector}


SERVICE = AgentService(
    application=APPLICATION,
    build_graph=_build_graph,
    respond=response,
    stream=_stream,
    default_port=8000,
    env_file=ENV_FILE,
    outcomes=_outcomes,
    app_extras=_extras,
)


async def build_app(
    services: ServiceContainer,
    lifecycle: ServiceLifecycle,
    *,
    context_builder: ContextBuilder | None = None,
    outcomes: OutcomeJournal | None = None,
    inspector: TurnInspector | None = None,
    tracing: AgentTracing | None = None,
) -> Any:
    """Build the agent's HTTP application; kept for callers of the previous shape.

    The graph is prepared once after the listener starts and before readiness.
    Each request brings its own caller: the entry point turns the bearer token into a
    request context before the graph runs. Unlike ``SERVICE``, which reads the context
    settings itself, ``context_builder`` here is the whole policy: ``None`` means none.
    ``main`` is the only caller that runs the service for real.

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

    def extras(services: ServiceContainer, graph: Callable[[], Any]) -> dict[str, Any]:
        # The caller's reader wins over the one SERVICE would build.
        return {"inspector": inspector} if inspector is not None else _extras(services, graph)

    service = dataclasses.replace(
        SERVICE,
        build_graph=_build_graph_with(context_builder),
        app_kwargs={**dict(SERVICE.app_kwargs), "outcomes": outcomes},
        app_extras=extras,
    )
    return await build_agent_app(service, services, lifecycle, tracing=tracing)


def main(argv: Sequence[str] | None = None) -> int:
    """Serve until stopped; a malformed context budget fails before anything starts."""
    try:
        contexts = ContextBuilder(load_context_budget(ENV_FILE))
    except ConfigurationError as error:
        return report_error(f"{APPLICATION}-serve", error, details=False)
    return run_agent(dataclasses.replace(SERVICE, build_graph=_build_graph_with(contexts)), argv)


if __name__ == "__main__":
    raise SystemExit(main())
