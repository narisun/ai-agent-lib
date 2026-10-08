"""Finding out why a service would not start, one dependency at a time.

``diagnose`` builds a service from its configuration, checks every adapter it
selects and reports each one. It changes nothing and writes nothing: it is
what ``agentlib doctor`` runs, and what a deployment can run before it sends
traffic to a new task.
"""

from __future__ import annotations

from ai_agent_lib_core.contracts import (
    DEFAULT_MODEL_ALIAS,
    AgentLibError,
    CheckResult,
    ServiceConfig,
)
from ai_agent_lib_core.di.container import ServiceContainer, fix_for
from ai_agent_lib_core.di.providers import ServiceProviders

__all__ = ["diagnose"]

_STARTUP = "startup"
_MODEL = "model"


def _model(services: ServiceContainer) -> CheckResult | None:
    """Check that the default model alias names a model its provider can build.

    No request is sent to the model: that would cost money on every check.
    """
    model = services.config.model
    if model.model_id is None and not model.aliases:
        return None  # a service without a model, such as an MCP server
    try:
        reference, provider = services.resolve_model(DEFAULT_MODEL_ALIAS)
        provider.create(reference.model_id)
    except AgentLibError as problem:
        return CheckResult(_MODEL, ok=False, detail=str(problem), fix=fix_for(problem))
    return CheckResult(
        _MODEL, ok=True, detail=f"{reference.provider} serves {reference.model_id!r}"
    )


async def diagnose(
    config: ServiceConfig, providers: ServiceProviders | None = None
) -> tuple[CheckResult, ...]:
    """Check everything ``config`` selects and return one result for each.

    If an adapter cannot even be built, the service cannot start and the one
    result says which adapter and why.

    Args:
        config: The service's resolved configuration.
        providers: The provider registry. Defaults to the installed adapters.
    """
    services = ServiceContainer(config, providers)
    try:
        await services.start()
    except AgentLibError as problem:
        return (CheckResult(_STARTUP, ok=False, detail=str(problem), fix=fix_for(problem)),)
    try:
        results = list(await services.check())
        model = _model(services)
        if model is not None:
            results.append(model)
        return tuple(results)
    finally:
        await services.aclose()
