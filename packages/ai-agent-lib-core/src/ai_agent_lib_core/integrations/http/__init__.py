"""HTTP serving. The only package in core that imports Starlette and uvicorn.

An agent's entry point, health and readiness routes for agents and MCP
servers, and a server loop that stops in order when the platform asks.
"""

from ai_agent_lib_core.integrations.http.agent import (
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
from ai_agent_lib_core.integrations.http.lifecycle import ServiceLifecycle, ServiceState
from ai_agent_lib_core.integrations.http.serving import serve

__all__ = [
    "HEALTH_PATH",
    "INVOKE_PATH",
    "READY_PATH",
    "STREAM_PATH",
    "AgentServices",
    "Run",
    "ServiceLifecycle",
    "ServiceState",
    "Stream",
    "SupportsCustomRoutes",
    "add_health_routes",
    "agent_app",
    "health_routes",
    "serve",
]
