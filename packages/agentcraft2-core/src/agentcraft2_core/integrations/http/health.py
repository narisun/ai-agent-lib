"""Liveness and readiness routes for any service: an agent or an MCP server.

Neither route asks for a token, because a load balancer has none, and neither
says anything about the service beyond one word.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from agentcraft2_core.integrations.http.lifecycle import ServiceLifecycle

__all__ = [
    "HEALTH_PATH",
    "READY_PATH",
    "SupportsCustomRoutes",
    "add_health_routes",
    "health_routes",
]

HEALTH_PATH = "/healthz"
READY_PATH = "/readyz"


class SupportsCustomRoutes(Protocol):
    """A server that takes extra HTTP routes, as the MCP SDK's server does."""

    def custom_route(self, path: str, methods: list[str]) -> Callable[[Any], Any]:
        """Return a decorator that adds a route outside the server's own protocol."""
        ...


async def _health(request: Request) -> Response:  # noqa: ARG001 - a route takes the request
    """Answer that the process is up. Says nothing about its dependencies."""
    return JSONResponse({"status": "ok"})


def _readiness(lifecycle: ServiceLifecycle) -> Callable[[Request], Awaitable[Response]]:
    async def ready(request: Request) -> Response:  # noqa: ARG001 - a route takes the request
        state = lifecycle.state.value
        return JSONResponse({"status": state}, status_code=200 if lifecycle.ready else 503)

    return ready


def health_routes(lifecycle: ServiceLifecycle) -> list[Route]:
    """Return the liveness and readiness routes of a service.

    Neither asks for a token: a load balancer has none. Neither says anything
    about the service beyond one word.
    """
    return [
        Route(HEALTH_PATH, _health, methods=["GET"]),
        Route(READY_PATH, _readiness(lifecycle), methods=["GET"]),
    ]


def add_health_routes(server: SupportsCustomRoutes, lifecycle: ServiceLifecycle) -> None:
    """Add the liveness and readiness routes to an MCP server.

    The MCP SDK serves these routes without the token check that guards the
    server's own endpoint.
    """
    server.custom_route(HEALTH_PATH, methods=["GET"])(_health)
    server.custom_route(READY_PATH, methods=["GET"])(_readiness(lifecycle))
