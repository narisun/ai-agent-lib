"""An HTTP entry point for an agent, and health routes for any service.

The entry point is deliberately small. It turns one HTTP request into one
governed run: it reads the bearer token, has the identity provider decide who
the caller is, and hands the caller's context and the request's input to a
function the agent's author wrote. That function runs the graph exactly as it
would anywhere else.

This module is the facade. The work lives in :mod:`.health` (liveness and
readiness), :mod:`.prelude` (reading a request and signing the caller in),
:mod:`.invoke` (the JSON routes), :mod:`.stream` (server-sent events) and
:mod:`.app` (composition); every name that was importable from here still is.
"""

from __future__ import annotations

from ai_agent_lib_core.integrations.http.app import _observed_run as _observed_run
from ai_agent_lib_core.integrations.http.app import agent_app
from ai_agent_lib_core.integrations.http.health import (
    HEALTH_PATH,
    READY_PATH,
    SupportsCustomRoutes,
    add_health_routes,
    health_routes,
)
from ai_agent_lib_core.integrations.http.invoke import INVOKE_PATH, Run
from ai_agent_lib_core.integrations.http.prelude import REQUEST_ID_HEADER, AgentServices
from ai_agent_lib_core.integrations.http.stream import STREAM_PATH, Stream

__all__ = [
    "HEALTH_PATH",
    "INVOKE_PATH",
    "READY_PATH",
    "REQUEST_ID_HEADER",
    "STREAM_PATH",
    "AgentServices",
    "Run",
    "Stream",
    "SupportsCustomRoutes",
    "add_health_routes",
    "agent_app",
    "health_routes",
]
