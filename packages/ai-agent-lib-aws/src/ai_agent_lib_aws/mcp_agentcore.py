"""Calling MCP servers hosted on Amazon Bedrock AgentCore runtimes.

A tool registry entry with a ``runtime_arn`` is reached at the platform's
invocation endpoint, which the entry derives from the ARN and the qualifier.
This connector implements the agent side's ``McpConnector`` protocol for such
entries: one shared HTTP client, an MCP session per call over the SDK's
Streamable HTTP transport, and on every request the credential the entry
asks for. With ``auth: bearer`` that is the token the identity provider
exchanged for the caller, in the ``Authorization`` header, for a runtime with
a JWT authorizer. With ``auth: sigv4`` each request is signed with the
service's own AWS credentials for the ``bedrock-agentcore`` service in the
runtime's region, for a runtime in IAM mode; the credentials are read from the
session on every request, so a refresh is honoured.

The platform answers with an ``Mcp-Session-Id`` header. The connector keeps
the last one per registered server and sends it on later requests to that
server, which is how a stateful runtime keeps serving the same microVM; a
stateless runtime accepts any. The MCP session the SDK opens per call is not
terminated on close, since the platform's session outlives it.

Errors come back as JSON-RPC errors in the platform's vocabulary, mostly over
HTTP 200. ``classify`` maps them for the agent side, which asks the connector
first: a refused token or a denied call is a ``PolicyDenied``, throttling and
a retryable session conflict are ``TransientError``, an unknown runtime is a
``ConfigurationError`` naming the registry field to check. A 401 or 403 at the
door, which is how a runtime in IAM mode answers a bad signature, is raised on
the HTTP client with its status kept, so the agent side's own mapping sees it.

Access: calling a runtime needs ``bedrock-agentcore:InvokeAgentRuntime`` on
the runtime ARN when ``auth`` is ``sigv4``. The deploy plan cannot derive it:
the tool registry is a document the service reads at startup, not part of its
deploy settings, so the grant is made by hand per registered runtime. With
``auth: bearer`` the runtime's JWT authorizer decides, and no IAM action is
needed.

This module needs the MCP SDK: the ``agentcore`` extra of ``ai-agent-lib-aws``.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import AsyncIterator, Generator, Mapping
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

import httpx2
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from pydantic import SecretStr

from ai_agent_lib_aws.session import AwsSessionFactory
from ai_agent_lib_core.contracts import (
    AgentLibError,
    ConfigurationError,
    NotStartedError,
    PolicyDenied,
    ServerEntry,
    TransientError,
    UpstreamError,
    ValidationFailed,
    describe,
)

__all__ = ["AgentCoreMcpConnector", "classify_agentcore_mcp_error"]

SESSION_HEADER = "mcp-session-id"
"""The header the platform returns, and expects back, for microVM affinity."""

_SIGNING_SERVICE = "bedrock-agentcore"
_REFUSED_STATUS = frozenset({401, 403})
_MAX_REMEMBERED_SESSIONS = 256
# The request headers that go into the signature, besides every ``mcp-*`` header.
# ``host`` is added by the signer from the URL; hop-by-hop headers stay out.
_SIGNED_HEADERS = frozenset({"accept", "content-type"})

# The platform's JSON-RPC error codes.
_UNAUTHORIZED = -32001
_ACCESS_DENIED = -32002
_THROTTLING = -32003
_NOT_FOUND = -32004
_CONFLICT = -32005
_VALIDATION = -32006
_RUNTIME_CLIENT_ERROR = -32010
_INTERNAL = -32603
_UPSTREAM_CODES: Mapping[int, str] = {
    _RUNTIME_CLIENT_ERROR: "RuntimeClientError",
    _INTERNAL: "InternalError",
}


@dataclass(frozen=True, slots=True)
class _Call:
    """The server and the caller's token of the call in progress."""

    server: ServerEntry
    token: SecretStr | None


# Bound by ``AgentCoreMcpConnector.connect`` for the enclosed block; the SDK's
# transport tasks, started inside that block, inherit it.
_CALL: ContextVar[_Call | None] = ContextVar("agentcraft2_agentcore_mcp_call", default=None)


class _SessionIds:
    """The platform session last returned per server, bounded so it cannot grow without end."""

    def __init__(self, limit: int) -> None:
        self._ids: OrderedDict[str, str] = OrderedDict()
        self._limit = limit

    def get(self, server_id: str) -> str | None:
        value = self._ids.get(server_id)
        if value is not None:
            self._ids.move_to_end(server_id)
        return value

    def remember(self, server_id: str, value: str) -> None:
        self._ids[server_id] = value
        self._ids.move_to_end(server_id)
        while len(self._ids) > self._limit:
            self._ids.popitem(last=False)


def _region_of(runtime_arn: str) -> str:
    """Return the region field of a runtime ARN; the entry validated its shape."""
    return runtime_arn.split(":")[3]


class _AgentCoreAuth(httpx2.Auth):
    """Puts the bound call's credential, and the server's platform session, on each request."""

    # The signature covers the body, so it must be read before the request is signed.
    requires_request_body = True
    requires_response_body = False

    def __init__(self, sessions: AwsSessionFactory, session_ids: _SessionIds) -> None:
        self._sessions = sessions
        self._session_ids = session_ids

    def auth_flow(
        self, request: httpx2.Request
    ) -> Generator[httpx2.Request, httpx2.Response, None]:
        call = _CALL.get()
        if call is None:
            yield request
            return
        server = call.server
        known = self._session_ids.get(server.id)
        if known is not None and SESSION_HEADER not in request.headers:
            request.headers[SESSION_HEADER] = known
        if server.auth == "sigv4" and server.runtime_arn is not None:
            self._sign(request, server.runtime_arn)
        elif call.token is not None:
            request.headers["Authorization"] = f"Bearer {call.token.get_secret_value()}"
        response = yield request
        returned = response.headers.get(SESSION_HEADER)
        if returned:
            self._session_ids.remember(server.id, returned)

    def _sign(self, request: httpx2.Request, runtime_arn: str) -> None:
        headers = {
            name: value
            for name, value in request.headers.items()
            if name.lower() in _SIGNED_HEADERS or name.lower().startswith("mcp-")
        }
        signable = AWSRequest(
            method=request.method, url=str(request.url), data=request.content, headers=headers
        )
        SigV4Auth(self._sessions.credentials(), _SIGNING_SERVICE, _region_of(runtime_arn)).add_auth(
            signable
        )
        for name, value in signable.headers.items():
            request.headers[name] = value


async def _refuse_at_the_door(response: httpx2.Response) -> None:
    """Raise for a 401 or a 403, keeping the status the MCP SDK would otherwise fold away.

    A runtime in IAM mode answers a bad signature with a 403 whose body is not
    a JSON-RPC error; raised here, the agent side can tell it from a server
    that cannot be reached.
    """
    if response.status_code in _REFUSED_STATUS:
        response.raise_for_status()


class AgentCoreMcpConnector:
    """Connects to MCP servers on AgentCore runtimes, signing or presenting a token per call.

    One ``httpx2.AsyncClient`` lives as long as the connector and serves every
    call to every runtime; the MCP bindings close it through ``aclose``. The
    MCP session is per call, as the SDK offers no session that outlives its
    context. The credential is per call too: ``connect`` binds the server
    entry and the caller's token for the enclosed block, and the client's
    ``httpx2.Auth`` reads them on each request. Nothing is stored on the
    shared client's default headers.

    Args:
        sessions: The pack's session factory; its credentials sign ``sigv4`` calls.
        timeout_seconds: How long one HTTP exchange may take; ``None`` for no
            limit. The composition root passes the configured tool call limit.
        transport: The HTTP transport, for a test; by default the real one.
    """

    def __init__(
        self,
        sessions: AwsSessionFactory,
        *,
        timeout_seconds: float | None,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self._sessions = sessions
        self._timeout_seconds = timeout_seconds
        self._session_ids = _SessionIds(_MAX_REMEMBERED_SESSIONS)
        self._closed = False
        self._http = httpx2.AsyncClient(
            auth=_AgentCoreAuth(sessions, self._session_ids),
            event_hooks={"response": [_refuse_at_the_door]},
            timeout=httpx2.Timeout(timeout_seconds),
            follow_redirects=False,
            # The environment is read by the configuration layer only.
            trust_env=False,
            transport=transport,
        )

    @property
    def timeout_seconds(self) -> float | None:
        """How long one HTTP exchange may take; ``None`` for no limit."""
        return self._timeout_seconds

    def session_id(self, server_id: str) -> str | None:
        """Return the platform session last returned for a server, or ``None``."""
        return self._session_ids.get(server_id)

    @asynccontextmanager
    async def connect(self, server: ServerEntry, token: SecretStr | None) -> AsyncIterator[Client]:
        """Open one MCP session with the runtime, over the shared HTTP client, for the block."""
        if self._closed:
            raise NotStartedError(f"the AgentCore connector for MCP server {server.id!r}")
        bound = _CALL.set(_Call(server, token))
        try:
            # The platform's session outlives the SDK's, so the SDK must not end it.
            async with Client(
                streamable_http_client(server.url, http_client=self._http, terminate_on_close=False)
            ) as client:
                yield client
        finally:
            _CALL.reset(bound)

    def credential_meta(self, token: SecretStr | None) -> Mapping[str, str]:  # noqa: ARG002
        """Return nothing: over HTTP the credential is in a header."""
        return {}

    def classify(self, error: BaseException, *, where: str) -> AgentLibError | None:
        """Map the platform's JSON-RPC error onto the library's taxonomy, or return ``None``."""
        return classify_agentcore_mcp_error(error, where=where)

    async def aclose(self) -> None:
        """Close the shared HTTP client. Safe to call more than once."""
        if self._closed:
            return
        self._closed = True
        await self._http.aclose()


def _jsonrpc_error(error: BaseException) -> tuple[int, str] | None:
    """Return the code and message of a JSON-RPC error the MCP SDK raised, if that is what it is."""
    if isinstance(error, AgentLibError):
        return None
    data: Any = getattr(error, "error", None)
    code: Any = getattr(data, "code", None) if data is not None else None
    if code is None:
        code = getattr(error, "code", None)
    if isinstance(code, bool) or not isinstance(code, int):
        return None
    message: Any = getattr(error, "message", None)
    return code, message if isinstance(message, str) else str(error)


def classify_agentcore_mcp_error(error: BaseException, *, where: str) -> AgentLibError | None:
    """Map a JSON-RPC error from the AgentCore platform onto the library's taxonomy.

    The platform's own text may repeat what the caller sent, so it goes into
    ``detail`` only; the message names the server and tool through ``where``.

    Args:
        error: The error the connection raised, with exception groups opened.
        where: Names the server and tool, for the message.

    Returns:
        The error to raise in its place, or ``None`` if ``error`` is not a
        JSON-RPC error with one of the platform's codes.
    """
    found = _jsonrpc_error(error)
    if found is None:
        return None
    code, message = found
    detail = describe(error)
    if code == _UNAUTHORIZED:
        return PolicyDenied(
            f"{where}: the runtime did not accept the caller's token",
            reason_code="server_refused_token",
            detail=detail,
        )
    if code == _ACCESS_DENIED:
        return PolicyDenied(
            f"{where}: the runtime denied the call", reason_code="server_denied", detail=detail
        )
    if code == _THROTTLING:
        return TransientError(
            f"{where}: the runtime is throttling calls",
            fix="try again later; ask for a higher AgentCore quota if this persists",
            detail=detail,
        )
    if code == _CONFLICT:
        if "please retry" in message.lower():
            return TransientError(
                f"{where}: another operation on the runtime session is in progress",
                detail=detail,
            )
        return UpstreamError(
            f"{where}: the runtime answered with a conflict",
            code="ConflictException",
            detail=detail,
        )
    if code == _NOT_FOUND:
        return ConfigurationError(
            f"{where}: the AgentCore runtime does not exist",
            expected="a runtime at the registered runtime_arn, with the registered qualifier",
            actual="ResourceNotFoundException from the platform",
            fix="check runtime_arn and qualifier of the server in the tool registry",
            detail=detail,
        )
    if code == _VALIDATION:
        return ValidationFailed(
            f"{where}: the runtime rejected the request as invalid",
            actual="ValidationException from the platform",
            detail=detail,
        )
    if code in _UPSTREAM_CODES:
        return UpstreamError(
            f"{where}: the runtime answered with an error",
            code=_UPSTREAM_CODES[code],
            detail=detail,
        )
    return None
