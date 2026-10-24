"""HTTP serving. The only package in core that imports Starlette and uvicorn.

An agent's entry point, health and readiness routes for agents and MCP
servers, and a server loop that stops in order when the platform asks.
"""

from agentcraft2_core.integrations.http.admission import (
    InMemoryThreadAdmission,
    SupportsActivity,
    ThreadAdmission,
)
from agentcraft2_core.integrations.http.agent import (
    HEALTH_PATH,
    INVOKE_PATH,
    READY_PATH,
    STREAM_PATH,
    AgentServices,
    Run,
    Stream,
    SupportsCustomRoutes,
    add_health_routes,
    agent_app,
    health_routes,
)
from agentcraft2_core.integrations.http.agentcore import (
    INVOCATIONS_PATH,
    PING_PATH,
    REQUEST_ID_HEADER_AGENTCORE,
    SESSION_HEADER,
    SessionBaggageMiddleware,
    agentcore_app,
)
from agentcraft2_core.integrations.http.composition import (
    PROFILES,
    AgentService,
    build_agent_app,
    read_question,
    require_serving_profile,
    run_agent,
)
from agentcraft2_core.integrations.http.entrypoint import (
    EXIT_INTERRUPTED,
    run_command,
    run_service,
)
from agentcraft2_core.integrations.http.lifecycle import ServiceLifecycle, ServiceState
from agentcraft2_core.integrations.http.outcomes import OutcomeJournal, SqliteOutcomeJournal
from agentcraft2_core.integrations.http.serving import serve

__all__ = [
    "EXIT_INTERRUPTED",
    "HEALTH_PATH",
    "INVOCATIONS_PATH",
    "INVOKE_PATH",
    "PING_PATH",
    "PROFILES",
    "READY_PATH",
    "REQUEST_ID_HEADER_AGENTCORE",
    "SESSION_HEADER",
    "STREAM_PATH",
    "AgentService",
    "AgentServices",
    "InMemoryThreadAdmission",
    "OutcomeJournal",
    "Run",
    "ServiceLifecycle",
    "ServiceState",
    "SessionBaggageMiddleware",
    "SqliteOutcomeJournal",
    "Stream",
    "SupportsActivity",
    "SupportsCustomRoutes",
    "ThreadAdmission",
    "add_health_routes",
    "agent_app",
    "agentcore_app",
    "build_agent_app",
    "health_routes",
    "read_question",
    "require_serving_profile",
    "run_agent",
    "run_command",
    "run_service",
    "serve",
]
