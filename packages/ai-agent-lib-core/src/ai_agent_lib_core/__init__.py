"""Core package of the Enterprise Agentic Platform library.

The names most applications need are available here::

    from ai_agent_lib_core import RequestContext, ServiceContainer

Everything else is reached through the sub-packages: ``contracts`` for ports
and value types, ``config`` for configuration, ``testing`` for fakes and
contract suites.
"""

from ai_agent_lib_core.contracts import (
    AgentLibError,
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
from ai_agent_lib_core.di import ServiceContainer, ServiceProviders, SyncServices
from ai_agent_lib_core.pipeline import bind_request_context

__all__ = [
    "AgentLibError",
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
