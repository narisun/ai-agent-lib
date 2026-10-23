"""The door of an MCP server whose identity provider trusts the hosting platform's perimeter.

A platform that authenticates callers before a request reaches the server, as
AgentCore Runtime does in its IAM mode, sends no bearer token; it sets headers
instead. The token door of the MCP SDK cannot be used then. This door refuses,
before the server sees them, requests that carry neither a bearer token nor
the headers the perimeter sets, so a request that did not come through the
perimeter is answered with 401 and nothing else. The verifier behind the door
still decides who the caller is, call by call.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Iterable, MutableMapping
from typing import Any

from agentcraft2_core.integrations.http.agentcore import PING_PATH
from agentcraft2_core.integrations.http.health import HEALTH_PATH, READY_PATH

__all__ = ["HEALTH_PATHS", "PerimeterDoor", "perimeter_refused"]

HEALTH_PATHS: tuple[str, ...] = (HEALTH_PATH, READY_PATH, PING_PATH)
"""The routes a load balancer or the platform reads without any credential."""

# The ASGI shapes, spelled out: this module wraps any server and imports no framework.
Scope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

_AUTHORIZATION = b"authorization"


def perimeter_refused() -> dict[str, str]:
    """Return the body a request refused at the perimeter door is answered with."""
    return {"error": "unauthorized", "reason": "perimeter_headers_missing"}


_REFUSED = json.dumps(perimeter_refused()).encode()


class PerimeterDoor:
    """An ASGI wrapper that lets through only requests the perimeter, or a token, vouches for.

    Args:
        app: The MCP server's ASGI application.
        required_headers: The headers the perimeter sets on every request it
            forwards, lower-case. A request that carries all of them passes;
            so does one that carries an ``Authorization`` header, which the
            verifier then checks. An identity that names no headers leaves
            the door only the ``Authorization`` header to look for.
        exempt_paths: Paths answered without any credential: the health routes.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        required_headers: Iterable[str],
        exempt_paths: Iterable[str] = HEALTH_PATHS,
    ) -> None:
        self._app = app
        self._required = tuple(name.lower().encode("latin-1") for name in required_headers)
        self._exempt = frozenset(exempt_paths)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Refuse an HTTP request the perimeter did not vouch for; pass everything else on."""
        if scope["type"] != "http" or scope.get("path") in self._exempt or self._vouched(scope):
            await self._app(scope, receive, send)
            return
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(_REFUSED)).encode("latin-1")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": _REFUSED})

    def _vouched(self, scope: Scope) -> bool:
        present = {key for key, value in scope.get("headers", ()) if value}
        if _AUTHORIZATION in present:
            return True
        return bool(self._required) and all(name in present for name in self._required)
