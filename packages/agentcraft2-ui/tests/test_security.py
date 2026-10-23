"""The session boundary and principal lookup run in process against a composed Runtime."""

import json
from typing import Any

import jwt
import pytest
from fastapi import HTTPException
from local_runtime import sign_in
from starlette.types import Message, Receive, Scope, Send

from agentcraft2_ui.integrations.runtime import Runtime
from agentcraft2_ui.integrations.security import (
    SessionBoundary,
    current_principal,
    session_token,
)

HOST = "127.0.0.1:8765"


class Downstream:
    """Record what reaches the protected application and answer minimally."""

    def __init__(self) -> None:
        self.calls: list[tuple[Scope, str]] = []

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        self.calls.append((scope, session_token.get()))
        if scope["type"] == "websocket":
            await send({"type": "websocket.accept"})
            return
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


def scope_for(
    path: str,
    *,
    kind: str = "http",
    method: str = "GET",
    host: str = HOST,
    origin: str | None = None,
    cookie: str | None = None,
    query: str = "",
    extra: tuple[tuple[bytes, bytes], ...] = (),
) -> Scope:
    headers: list[tuple[bytes, bytes]] = [(b"host", host.encode()), *extra]
    if origin is not None:
        headers.append((b"origin", origin.encode()))
    if cookie is not None:
        headers.append((b"cookie", cookie.encode()))
    scope: dict[str, Any] = {
        "type": kind,
        "path": path,
        "headers": headers,
        "query_string": query.encode(),
    }
    if kind == "http":
        scope["method"] = method
    return scope


async def call(boundary: SessionBoundary, scope: Scope) -> list[Message]:
    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        sent.append(message)

    await boundary(scope, receive, send)
    return sent


def status_and_body(sent: list[Message]) -> tuple[int, dict[str, str], Any]:
    start = sent[0]
    assert start["type"] == "http.response.start"
    headers = {key.decode(): value.decode() for key, value in start["headers"]}
    body = b"".join(message.get("body", b"") for message in sent[1:])
    return start["status"], headers, json.loads(body) if body.startswith(b"{") else body


@pytest.fixture
def downstream() -> Downstream:
    return Downstream()


@pytest.fixture
def boundary(runtime: Runtime, downstream: Downstream) -> SessionBoundary:
    return SessionBoundary(downstream, resources=runtime)


async def test_missing_cookie_is_unauthorized_json_for_api_and_a_redirect_for_pages(
    boundary: SessionBoundary, downstream: Downstream
) -> None:
    status, headers, body = status_and_body(await call(boundary, scope_for("/api/threads")))
    assert (status, body) == (401, {"detail": "Sign in to the local UI"})
    assert headers["cache-control"] == "no-store"
    assert "frame-ancestors 'none'" in headers["content-security-policy"]
    status, headers, _ = status_and_body(
        await call(boundary, scope_for("/catalog", query="search=x"))
    )
    assert status == 303
    assert headers["location"] == "/login?next=%2Fcatalog%3Fsearch%3Dx"
    assert downstream.calls == []


async def test_bad_or_revoked_tokens_are_unauthorized(
    runtime: Runtime, boundary: SessionBoundary, downstream: Downstream
) -> None:
    status, _, body = status_and_body(
        await call(boundary, scope_for("/api/threads", cookie="agentcraft2_session=forged"))
    )
    assert (status, body) == (401, {"detail": "Sign in to the local UI"})
    token = await runtime.store.new_session(runtime.identity)
    status, _, _ = status_and_body(
        await call(boundary, scope_for("/api/threads", cookie=f"agentcraft2_session={token}"))
    )
    assert status == 200
    await runtime.store.revoke(token)
    status, _, _ = status_and_body(
        await call(boundary, scope_for("/api/threads", cookie=f"agentcraft2_session={token}"))
    )
    assert status == 401
    assert len(downstream.calls) == 1


async def test_wrong_host_and_cross_origin_mutations_are_forbidden(
    runtime: Runtime, boundary: SessionBoundary, downstream: Downstream
) -> None:
    token = await runtime.store.new_session(runtime.identity)
    cookie = f"agentcraft2_session={token}"
    status, _, body = status_and_body(
        await call(boundary, scope_for("/api/threads", host="localhost:8765", cookie=cookie))
    )
    assert (status, body) == (403, {"detail": "Unexpected host"})
    status, _, body = status_and_body(
        await call(boundary, scope_for("/api/threads", method="POST", cookie=cookie))
    )
    assert (status, body) == (403, {"detail": "Same-origin request required"})
    status, _, _ = status_and_body(
        await call(
            boundary,
            scope_for(
                "/api/threads",
                method="POST",
                cookie=cookie,
                origin=runtime.settings.origin,
            ),
        )
    )
    assert status == 200
    assert len(downstream.calls) == 1


async def test_chainlit_identity_must_match_the_parent_session(
    runtime: Runtime, boundary: SessionBoundary, downstream: Downstream
) -> None:
    token = await runtime.store.new_session(runtime.identity)
    other = jwt.encode({"identifier": "someone-else"}, runtime.auth_secret, algorithm="HS256")
    status, _, body = status_and_body(
        await call(
            boundary,
            scope_for(
                "/chat/project/threads",
                cookie=f"agentcraft2_session={token}; agentcraft2_chat={other}",
            ),
        )
    )
    assert (status, body) == (401, {"detail": "Session identities differ"})
    forged = jwt.encode({"identifier": runtime.identity}, "x" * 48, algorithm="HS256")
    status, _, body = status_and_body(
        await call(
            boundary,
            scope_for(
                "/chat/project/threads",
                cookie=f"agentcraft2_session={token}; agentcraft2_chat={forged}",
            ),
        )
    )
    assert (status, body) == (401, {"detail": "Sign in to the local UI"})
    matching = jwt.encode({"identifier": runtime.identity}, runtime.auth_secret, algorithm="HS256")
    status, _, _ = status_and_body(
        await call(
            boundary,
            scope_for(
                "/chat/project/threads",
                cookie=f"agentcraft2_session={token}; agentcraft2_chat={matching}",
                extra=((b"authorization", b"Bearer leaked"), (b"x-agentcraft2-user", b"root")),
            ),
        )
    )
    assert status == 200
    [(seen, bound_token)] = downstream.calls
    assert bound_token == token
    assert {key for key, _ in seen["headers"]} == {b"host", b"cookie"}
    assert session_token.get() == ""


async def test_websocket_without_a_session_closes_with_4403(
    runtime: Runtime, boundary: SessionBoundary, downstream: Downstream
) -> None:
    origin = runtime.settings.origin
    sent = await call(boundary, scope_for("/chat/ws/socket.io/", kind="websocket", origin=origin))
    assert sent == [{"type": "websocket.close", "code": 4403}]
    token = await runtime.store.new_session(runtime.identity)
    sent = await call(
        boundary,
        scope_for("/chat/ws/socket.io/", kind="websocket", cookie=f"agentcraft2_session={token}"),
    )
    assert sent == [{"type": "websocket.close", "code": 4403}]
    sent = await call(
        boundary,
        scope_for(
            "/chat/ws/socket.io/",
            kind="websocket",
            origin=origin,
            cookie=f"agentcraft2_session={token}",
        ),
    )
    assert sent == [{"type": "websocket.accept"}]
    assert downstream.calls[0][1] == token


async def test_public_assets_and_disabled_features(
    runtime: Runtime, boundary: SessionBoundary, downstream: Downstream
) -> None:
    status, _, _ = status_and_body(await call(boundary, scope_for("/healthz")))
    assert status == 200
    assert downstream.calls[0][1] == ""
    token = await runtime.store.new_session(runtime.identity)
    status, _, body = status_and_body(
        await call(boundary, scope_for("/chat/mcp", cookie=f"agentcraft2_session={token}"))
    )
    assert (status, body) == (404, {"detail": "This feature is not enabled"})


async def test_current_principal_requires_a_live_session(runtime: Runtime) -> None:
    with pytest.raises(HTTPException) as refused:
        await current_principal(runtime)
    assert refused.value.status_code == 401
    token = await sign_in(runtime)
    principal = await current_principal(runtime)
    assert (principal.subject, principal.tenant) == ("dev-user", "dev-tenant")
    assert principal.authenticated_by == "ui-local-session"
    await runtime.store.revoke(token)
    with pytest.raises(HTTPException) as revoked:
        await current_principal(runtime)
    assert revoked.value.status_code == 401
