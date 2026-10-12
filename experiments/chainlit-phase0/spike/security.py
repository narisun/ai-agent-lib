"""Parent session boundary for HTTP and WebSocket traffic, including Chainlit."""

import contextvars
from http.cookies import SimpleCookie

import jwt
from fastapi import HTTPException
from starlette.responses import JSONResponse, RedirectResponse

from spike.settings import Settings
from spike.store import Store

session_token: contextvars.ContextVar[str] = contextvars.ContextVar("parent_session", default="")


def current_owner(store: Store) -> str:
    """Revalidate the parent session on each application/data-layer operation."""
    return store.owner(session_token.get())


def local_asset_policy(downstream):
    """Add a local-only asset policy without changing the upstream bundle."""

    async def send(message):
        if message["type"] == "http.response.start":
            # Stock HTML references optional public font/math styles. Block
            # external assets: this sample uses system fonts and no LaTeX.
            policy = (
                "default-src 'self'; script-src 'self' 'unsafe-inline' 'unsafe-eval'; "
                "style-src 'self' 'unsafe-inline'; font-src 'self' data:; "
                "img-src 'self' data: blob:; connect-src 'self' ws://127.0.0.1:8765; "
                "worker-src 'self' blob:; frame-ancestors 'none'; base-uri 'self'; "
                "form-action 'self'"
            )
            message = {
                **message,
                "headers": [
                    *message.get("headers", []),
                    (b"content-security-policy", policy.encode()),
                    (b"referrer-policy", b"same-origin"),
                ],
            }
        await downstream(message)

    return send


class SessionBoundary:
    """Authenticate parent sessions before all mounted application traffic."""

    def __init__(self, app, *, store: Store, settings: Settings):
        self.app, self.store, self.settings = app, store, settings

    async def __call__(self, scope, receive, send):
        """Validate the host, origin, parent session, and matching chat identity."""
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        send = local_asset_policy(send)
        headers = dict(scope["headers"])
        path = scope["path"]
        is_socket = scope["type"] == "websocket"
        try:
            expected_host = self.settings.origin.removeprefix("http://")
            if headers.get(b"host", b"").decode() != expected_host:
                raise HTTPException(403, "Unexpected host")
            mutating = scope.get("method", "GET") not in ("GET", "HEAD", "OPTIONS")
            if (
                (is_socket or mutating)
                and not path.startswith("/fixture/")
                and headers.get(b"origin", b"").decode() != self.settings.origin
            ):
                raise HTTPException(403, "Same-origin request required")
            if path in ("/login", "/healthz") or path.startswith("/fixture/"):
                return await self.app(scope, receive, send)
            cookie = SimpleCookie()
            cookie.load(headers.get(b"cookie", b"").decode())
            token = cookie["phase0_session"].value if "phase0_session" in cookie else ""
            owner = self.store.owner(token)
            # Bind Chainlit's session to the parent's identity. A valid JWT for Bob
            # must never be accepted alongside Alice's parent session.
            if "phase0_chainlit" in cookie and path != "/chat/auth/header":
                claims = jwt.decode(
                    cookie["phase0_chainlit"].value, self.settings.auth_secret, algorithms=["HS256"]
                )
                if claims.get("identifier") != owner:
                    raise HTTPException(401, "Session identities differ")
            scope = {
                **scope,
                "headers": [
                    (key, value)
                    for key, value in scope["headers"]
                    if key.lower() not in (b"x-phase0-user", b"authorization")
                ]
                + [(b"x-phase0-user", owner.encode())],
            }
            context = session_token.set(token)
            try:
                return await self.app(scope, receive, send)
            finally:
                session_token.reset(context)
        except jwt.PyJWTError:
            error = HTTPException(401, "Invalid chat session")
        except HTTPException as caught:
            error = caught
        if is_socket:
            return await send({"type": "websocket.close", "code": 4403})
        if error.status_code == 401 and path in ("/", "/chat", "/chat/"):
            response = RedirectResponse("/login", status_code=303)
        else:
            response = JSONResponse({"detail": error.detail}, status_code=error.status_code)
        return await response(scope, receive, send)
