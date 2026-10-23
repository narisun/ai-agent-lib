"""Authenticate the parent session before every HTTP and WebSocket route."""

import contextvars
from http.cookies import CookieError, SimpleCookie
from urllib.parse import urlencode

import jwt
from fastapi import HTTPException
from starlette.responses import JSONResponse, RedirectResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from agentcraft2_core.contracts import Principal
from agentcraft2_ui.contracts.queries import ContractError
from agentcraft2_ui.integrations.navigation import is_page_path, login_destination
from agentcraft2_ui.integrations.runtime import Runtime

session_token: contextvars.ContextVar[str] = contextvars.ContextVar("ui_session", default="")


async def current_principal(resources: Runtime) -> Principal:
    """Revalidate the stored session once per request or framework callback.

    Callers resolve the principal when they start handling a request and pass it
    down; progress ticks and other per-token work never call this again.
    """
    try:
        await resources.store.session(session_token.get(), resources.identity)
    except ContractError:
        raise HTTPException(401, "Sign in to the local UI") from None
    return resources.settings.identity.principal()


class SessionBoundary:
    """Reject untrusted hosts/origins and bind Chainlit identity to the parent cookie."""

    def __init__(self, app: ASGIApp, *, resources: Runtime) -> None:
        self.app, self.resources = app, resources

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Protect mounted history, asset, authentication and WebSocket routes alike."""
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        send = response_headers(send, self.resources.settings.origin)
        headers = dict(scope["headers"])
        path = scope["path"]
        is_socket = scope["type"] == "websocket"
        try:
            origin = self.resources.settings.origin
            if headers.get(b"host", b"").decode("latin1") != origin.removeprefix("http://"):
                raise HTTPException(403, "Unexpected host")
            mutating = scope.get("method", "GET") not in {"GET", "HEAD", "OPTIONS"}
            if (is_socket or mutating) and headers.get(b"origin", b"").decode("latin1") != origin:
                raise HTTPException(403, "Same-origin request required")
            if path in {
                "/login",
                "/healthz",
                "/ui/forms.js",
                "/ui/agent-lab.svg",
                "/favicon",
                "/ui/theme.css",
                "/ui/theme.js",
            }:
                await self.app(scope, receive, send)
                return
            cookie = SimpleCookie()
            cookie.load(headers.get(b"cookie", b"").decode("latin1"))
            token = cookie["agentcraft2_session"].value if "agentcraft2_session" in cookie else ""
            await self.resources.store.session(token, self.resources.identity)
            if path == "/chat/mcp" or path.startswith("/chat/project/file"):
                raise HTTPException(404, "This feature is not enabled")
            if "agentcraft2_chat" in cookie and path != "/chat/auth/header":
                claims = jwt.decode(
                    cookie["agentcraft2_chat"].value,
                    self.resources.auth_secret,
                    algorithms=["HS256"],
                )
                if claims.get("identifier") != self.resources.identity:
                    raise HTTPException(401, "Session identities differ")
            # No browser credential or authority header is ever forwarded to an agent.
            scope = {
                **scope,
                "headers": [
                    (key, value)
                    for key, value in scope["headers"]
                    if key.lower() not in {b"authorization", b"x-agentcraft2-user"}
                ],
            }
            context = session_token.set(token)
            try:
                await self.app(scope, receive, send)
            finally:
                session_token.reset(context)
            return
        except (ContractError, jwt.PyJWTError, CookieError):
            error = HTTPException(401, "Sign in to the local UI")
        except HTTPException as caught:
            error = caught
        if is_socket:
            await send({"type": "websocket.close", "code": 4403})
            return
        response: JSONResponse | RedirectResponse
        if (
            error.status_code == 401
            and scope.get("method") in {"GET", "HEAD"}
            and is_page_path(path)
        ):
            query = scope.get("query_string", b"").decode("latin1")
            destination = login_destination(path + ("?" + query if query else ""))
            response = RedirectResponse(
                "/login?" + urlencode({"next": destination}), status_code=303
            )
        else:
            response = JSONResponse({"detail": error.detail}, status_code=error.status_code)
        await response(scope, receive, send)


def response_headers(downstream: Send, origin: str) -> Send:
    """Keep local pages uncacheable and block remote scripts, fonts and frames."""

    async def secured_send(message: Message) -> None:
        if message["type"] == "http.response.start":
            policy = (
                "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                "style-src 'self' 'unsafe-inline'; font-src 'self' data:; "
                "img-src 'self' data: blob:; connect-src 'self' "
                + origin.replace("http://", "ws://")
                + "; "
                "worker-src 'self' blob:; frame-ancestors 'none'; base-uri 'self'; "
                "form-action 'self'; object-src 'none'"
            )
            message = {
                **message,
                "headers": [
                    *message.get("headers", []),
                    (b"content-security-policy", policy.encode()),
                    (b"referrer-policy", b"no-referrer"),
                    (b"cache-control", b"no-store"),
                    (b"x-content-type-options", b"nosniff"),
                ],
            }
        await downstream(message)

    return secured_send
