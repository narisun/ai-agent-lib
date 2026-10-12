"""Authenticate the parent session before every HTTP and WebSocket route."""

import contextvars
from http.cookies import CookieError, SimpleCookie

import jwt
from fastapi import HTTPException
from starlette.responses import JSONResponse, RedirectResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ai_agent_lib_core.contracts import Principal
from ai_agent_lib_ui.contracts.queries import ContractError
from ai_agent_lib_ui.integrations.runtime import Runtime, runtime

session_token: contextvars.ContextVar[str] = contextvars.ContextVar("ui_session", default="")


def current_principal() -> Principal:
    """Revalidate stored sessions on each callback, including already-open sockets."""
    resources = runtime()
    try:
        resources.store.session(session_token.get(), resources.identity)
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
            if path in {"/login", "/healthz", "/ui/forms.js"}:
                await self.app(scope, receive, send)
                return
            cookie = SimpleCookie()
            cookie.load(headers.get(b"cookie", b"").decode("latin1"))
            token = cookie["agentlib_session"].value if "agentlib_session" in cookie else ""
            self.resources.store.session(token, self.resources.identity)
            if path == "/chat/mcp" or path.startswith("/chat/project/file"):
                raise HTTPException(404, "This feature is not enabled")
            if "agentlib_chat" in cookie and path != "/chat/auth/header":
                claims = jwt.decode(
                    cookie["agentlib_chat"].value, self.resources.auth_secret, algorithms=["HS256"]
                )
                if claims.get("identifier") != self.resources.identity:
                    raise HTTPException(401, "Session identities differ")
            # No browser credential or authority header is ever forwarded to an agent.
            scope = {
                **scope,
                "headers": [
                    (key, value)
                    for key, value in scope["headers"]
                    if key.lower() not in {b"authorization", b"x-agentlib-user"}
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
        response = (
            RedirectResponse("/login", status_code=303)
            if error.status_code == 401 and path in {"/", "/chat", "/chat/", "/conversations"}
            else JSONResponse({"detail": error.detail}, status_code=error.status_code)
        )
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
