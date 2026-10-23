"""Core package of the Enterprise Agentic Platform library.

The names most applications need are available here::

    from agentcraft2_core import RequestContext, ServiceContainer

Everything else is reached through the sub-packages: ``contracts`` for ports
and value types, ``config`` for configuration, ``testing`` for fakes and
contract suites.
"""

from typing import TYPE_CHECKING, Any

from agentcraft2_core.contracts import (
    AgentCraft2Error,
    BudgetExceeded,
    Classification,
    ConfigurationError,
    CredentialsExpiredError,
    ExecutionPaused,
    IntegrityError,
    PolicyDenied,
    Principal,
    RequestContext,
    Scope,
    ServiceConfig,
    StructuredOutput,
    TransientError,
    ValidationFailed,
)

if TYPE_CHECKING:
    from agentcraft2_core.di import ServiceContainer, ServiceProviders, SyncServices
    from agentcraft2_core.pipeline import bind_request_context


def __getattr__(name: str) -> Any:
    """Load runtime helpers on first access, keeping contract-only clients light.

    The public import names are unchanged. Importing a shared contract no longer
    constructs the dependency-injection module tree or imports model frameworks.
    """
    if name in {"ServiceContainer", "ServiceProviders", "SyncServices"}:
        from agentcraft2_core import di

        return getattr(di, name)
    if name == "bind_request_context":
        from agentcraft2_core.pipeline import bind_request_context

        return bind_request_context
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))


__all__ = [
    "AgentCraft2Error",
    "BudgetExceeded",
    "Classification",
    "ConfigurationError",
    "CredentialsExpiredError",
    "ExecutionPaused",
    "IntegrityError",
    "PolicyDenied",
    "Principal",
    "RequestContext",
    "Scope",
    "ServiceConfig",
    "ServiceContainer",
    "ServiceProviders",
    "StructuredOutput",
    "SyncServices",
    "TransientError",
    "ValidationFailed",
    "bind_request_context",
]
