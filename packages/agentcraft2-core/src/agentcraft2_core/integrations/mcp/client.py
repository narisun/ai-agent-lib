"""The agent side: registered MCP tools as governed LangChain tools.

A server may list its tools under a prefix the registry does not use, for
example ``target___tool`` for a tool reached through an AgentCore gateway.
The prefix is the server entry's ``tool_prefix``: ``load_mcp_tools`` looks a
registered tool up in the server's listing under the prefixed name, and the
governed tool calls the server with that name, while the tool's own name, the
registry check and the policy keep the registered name. Nothing is stripped
anywhere else. ``verify_registration`` runs on the server itself, where the
names carry no prefix, and a schema pin fingerprints the input schema, not
the name, so a prefixed listing pins exactly as an unprefixed one does.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Generator, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from contextvars import ContextVar
from typing import Any, Protocol, runtime_checkable

import httpx2
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool
from mcp import Client, MCPError
from mcp.client.streamable_http import streamable_http_client
from mcp_types import Tool
from opentelemetry import propagate
from pydantic import PrivateAttr, SecretStr

from agentcraft2_core.contracts import (
    AgentCraft2Error,
    IntegrityError,
    NotStartedError,
    PolicyDenied,
    RequestContext,
    ServerEntry,
    TokenExchanger,
    ToolEntry,
    TransientError,
    ValidationFailed,
    describe,
    schema_fingerprint,
)
from agentcraft2_core.contracts.structured import model_safe_name, schema_mismatch
from agentcraft2_core.integrations.langgraph.bridge import LoopBridge
from agentcraft2_core.integrations.langgraph.context import current_request_context
from agentcraft2_core.integrations.langgraph.tools import GovernedToolBase
from agentcraft2_core.integrations.mcp.wire import (
    META_CLASSIFICATION_CEILING,
    META_REQUEST_ID,
    META_THREAD_ID,
    is_error_result,
    mcp_result_text,
)
from agentcraft2_core.pipeline import Pipeline, ToolCall, ToolStage

__all__ = [
    "GovernedMcpTool",
    "HttpMcpConnector",
    "McpConnector",
    "SupportsErrorClassification",
    "load_mcp_tools",
]

_MAX_REPORTED = 5
_MAX_PAGES = 100


class McpConnector(Protocol):
    """Opens a connection to a registered MCP server."""

    def connect(
        self, server: ServerEntry, token: SecretStr | None
    ) -> AbstractAsyncContextManager[Client]:
        """Return a connected client, presenting ``token`` as the caller's credential."""
        ...

    def credential_meta(self, token: SecretStr | None) -> Mapping[str, str]:
        """Return request metadata that carries the token, for transports without headers."""
        ...


@runtime_checkable
class SupportsErrorClassification(Protocol):
    """Optional: a connector that knows what its transport's errors mean.

    A platform in front of a server answers in its own vocabulary, for example
    JSON-RPC error codes for a refused token or a throttled call. The connector
    for that platform maps them; the agent side asks it first and falls back
    to its own mapping for anything it leaves alone.
    """

    def classify(self, error: BaseException, *, where: str) -> AgentCraft2Error | None:
        """Return the library error for ``error``, or ``None`` to leave it to the caller.

        Args:
            error: The error the connection raised, with exception groups opened.
            where: Names the server and tool for the message. The server's own
                text never goes into the message; it may go into ``detail``.
        """
        ...


def _classified(
    connector: McpConnector, error: BaseException, *, where: str
) -> AgentCraft2Error | None:
    """Ask the connector what ``error`` means, when it can say."""
    if isinstance(connector, SupportsErrorClassification):
        return connector.classify(error, where=where)
    return None


_REFUSED_STATUS = frozenset({401, 403})

# The token of the call in progress. ``HttpMcpConnector.connect`` binds it for the
# enclosed block; the SDK's transport tasks, started inside that block, inherit it.
_CALL_TOKEN: ContextVar[SecretStr | None] = ContextVar("agentcraft2_mcp_call_token", default=None)


class _TokenOfTheCall(httpx2.Auth):
    """Puts the token bound for the current call on each request of the shared client."""

    requires_request_body = False
    requires_response_body = False

    def auth_flow(
        self, request: httpx2.Request
    ) -> Generator[httpx2.Request, httpx2.Response, None]:
        token = _CALL_TOKEN.get()
        if token is not None:
            request.headers["Authorization"] = f"Bearer {token.get_secret_value()}"
        yield request


async def _refuse_at_the_door(response: httpx2.Response) -> None:
    """Raise for a 401 or a 403, which the MCP SDK would otherwise fold into a generic error.

    The SDK answers any 4xx or 5xx with an ``MCPError`` that no longer says which
    status it was. Raised here, on the HTTP client, the status is kept, so the
    caller can tell a refused token from a server that cannot be reached.
    """
    if response.status_code in _REFUSED_STATUS:
        response.raise_for_status()


class HttpMcpConnector:
    """Connects over Streamable HTTP, with the token in the ``Authorization`` header.

    What is reused: one ``httpx2.AsyncClient``, with its connection pool, TLS
    sessions and timeouts, lives as long as the connector and serves every call
    to every server. The container closes it through ``aclose``.

    What is per call: the MCP session. The SDK's ``Client`` opens one over the
    shared HTTP client, runs the initialize handshake, makes the call and ends
    the session when the block closes; the SDK offers no session that outlives
    its context. The caller's token is per call as well, since each request is
    made for a different caller: ``connect`` binds it for the enclosed block and
    an ``httpx2.Auth`` puts it on each request. It is never stored on the shared
    client's default headers.

    Args:
        timeout_seconds: How long one HTTP exchange may take; ``None`` for no
            limit. The composition root passes the configured tool call limit.
        transport: The HTTP transport, for a test; by default the real one.
    """

    def __init__(
        self,
        *,
        timeout_seconds: float | None,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self._timeout_seconds = timeout_seconds
        self._closed = False
        self._http = httpx2.AsyncClient(
            auth=_TokenOfTheCall(),
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

    @asynccontextmanager
    async def connect(self, server: ServerEntry, token: SecretStr | None) -> AsyncIterator[Client]:
        """Open one MCP session over the shared HTTP client for the enclosed block."""
        if self._closed:
            raise NotStartedError(f"the HTTP connector for MCP server {server.id!r}")
        bound = _CALL_TOKEN.set(token)
        try:
            async with Client(streamable_http_client(server.url, http_client=self._http)) as client:
                yield client
        finally:
            _CALL_TOKEN.reset(bound)

    def credential_meta(self, token: SecretStr | None) -> Mapping[str, str]:  # noqa: ARG002
        """Return nothing: over HTTP the token is in a header."""
        return {}

    async def aclose(self) -> None:
        """Close the shared HTTP client. Safe to call more than once."""
        if self._closed:
            return
        self._closed = True
        await self._http.aclose()


def _refused_at_the_door(error: BaseException) -> bool:
    """Return whether the server answered that it does not accept the token shown."""
    return (
        isinstance(error, httpx2.HTTPStatusError) and error.response.status_code in _REFUSED_STATUS
    )


def _unwrap(error: BaseException) -> BaseException:
    """Return the one real error inside nested exception groups, if there is exactly one."""
    while isinstance(error, BaseExceptionGroup) and len(error.exceptions) == 1:
        error = error.exceptions[0]
    return error


class GovernedMcpTool(GovernedToolBase):
    """A registered MCP tool, called through the agent's tool pipeline.

    To a model and to a ``ToolNode`` it is an ordinary tool. Each call is
    checked against the registries and the policy, the caller's identity is
    exchanged for a token bound to the server, and the result comes back
    framed as untrusted data.

    There is no wrapped LangChain tool here: the arguments are the MCP tool's
    input schema, and the call goes to the server.
    """

    _server: ServerEntry = PrivateAttr()
    _registered_name: str = PrivateAttr()
    _mcp_name: str = PrivateAttr()
    _connector: McpConnector = PrivateAttr()
    _exchanger: TokenExchanger | None = PrivateAttr(default=None)
    _validator: Draft202012Validator = PrivateAttr()
    _input_schema: dict[str, Any] = PrivateAttr(default_factory=dict)

    @classmethod
    def build(
        cls,
        *,
        server: ServerEntry,
        entry: ToolEntry,
        offered: Tool,
        connector: McpConnector,
        exchanger: TokenExchanger | None,
        pipeline: Pipeline[ToolStage, ToolCall, Any],
        bridge: Callable[[], LoopBridge],
    ) -> GovernedMcpTool:
        """Return the governed tool for one registered tool of one server."""
        try:
            Draft202012Validator.check_schema(offered.input_schema)
        except SchemaError:
            raise IntegrityError(
                f"tool {entry.name!r} of MCP server {server.id!r} has an invalid input schema"
            ) from None
        tool = cls(
            name=model_safe_name(entry.name),
            description=offered.description or entry.description or entry.name,
            args_schema=offered.input_schema,
            read_only=entry.read_only,
        )
        tool._server = server
        tool._registered_name = entry.name
        # The server lists the tool under the prefix; the registry and the policy do not.
        tool._mcp_name = server.tool_prefix + entry.name
        tool._connector = connector
        tool._exchanger = exchanger
        tool._validator = Draft202012Validator(offered.input_schema)
        tool._input_schema = dict(offered.input_schema)
        tool._handler = pipeline.bind(tool._call_inner)
        tool._bridge = bridge
        return tool

    def _argument_names(self) -> Sequence[str]:
        properties = self._input_schema.get("properties")
        return list(properties) if isinstance(properties, Mapping) else []

    def _make_call(self, arguments: dict[str, Any], config: RunnableConfig) -> ToolCall:
        return ToolCall(
            context=current_request_context(),
            tool=self._registered_name,
            arguments=arguments,
            read_only=self.read_only,
            payload=config,
            server=self._server.id,
        )

    async def _call_inner(self, call: ToolCall) -> Any:
        context = call.context
        if not isinstance(context, RequestContext):
            raise TypeError("the tool call reached the MCP server without a request context")
        arguments = dict(call.arguments)
        mismatch = schema_mismatch(self._validator, arguments)
        if mismatch is not None:
            raise ValidationFailed(
                f"the arguments for tool {self._registered_name!r} do not match its input schema",
                expected=mismatch.expected,
                actual=mismatch.actual,
                detail=mismatch.detail,
            )
        audience = self._server.audience or self._server.id
        token = (
            await self._exchanger.exchange(context, audience)
            if self._exchanger is not None
            else None
        )
        meta: dict[str, Any] = {
            META_REQUEST_ID: context.request_id,
            META_THREAD_ID: context.thread_id,
            META_CLASSIFICATION_CEILING: context.classification_ceiling.name.lower(),
            **self._connector.credential_meta(token),
        }
        # W3C trace context, so the server's spans join this request's trace.
        propagate.inject(meta)
        where = f"MCP server {self._server.id!r}, tool {self._registered_name!r}"
        try:
            async with self._connector.connect(self._server, token) as client:
                result = await client.call_tool(self._mcp_name, arguments, meta=meta)  # type: ignore[arg-type]
        except BaseException as raised:
            error = _unwrap(raised)
            # The connector knows its platform's vocabulary; it is asked first.
            classified = _classified(self._connector, error, where=where)
            if isinstance(classified, PolicyDenied):
                raise classified from None
            if classified is not None:
                raise classified from error
            if isinstance(error, MCPError):
                # The server's own text can repeat what the caller sent, so it is
                # kept for a developer's machine only, never in the message or facts.
                raise AgentCraft2Error(
                    f"{where}: the server answered with an error",
                    actual=f"an MCP error of type {type(error).__name__}",
                    fix="look at the server's log line for this request ID",
                    detail=describe(error),
                ) from None
            if _refused_at_the_door(error):
                # Not a passing fault: the same token would be refused again.
                raise PolicyDenied(
                    f"{where}: the server did not accept the caller's token",
                    reason_code="server_refused_token",
                ) from None
            if isinstance(error, httpx2.HTTPError | OSError | TimeoutError):
                raise TransientError(
                    f"{where}: the server could not be reached",
                    expected=f"an MCP server answering at {self._server.url}",
                    actual=type(error).__name__,
                    detail=describe(error),
                ) from error
            raise
        text = mcp_result_text(result)
        if is_error_result(result):
            call.evidence.add(tool_error=True)
            return f"The tool reported an error: {text}"
        return text


async def _list_tools(client: Client) -> dict[str, Tool]:
    offered: dict[str, Tool] = {}
    cursor: str | None = None
    for _ in range(_MAX_PAGES):
        page = await client.list_tools(cursor=cursor)
        offered.update({tool.name: tool for tool in page.tools})
        cursor = page.next_cursor
        if not cursor:
            return offered
    raise IntegrityError("the MCP server's tool listing does not end")


async def load_mcp_tools(
    *,
    server: ServerEntry,
    connector: McpConnector,
    exchanger: TokenExchanger | None,
    pipeline: Pipeline[ToolStage, ToolCall, Any],
    bridge: Callable[[], LoopBridge],
    require_pins: bool,
) -> list[BaseTool]:
    """Connect to a registered server and return its registered tools, governed.

    Only tools that are in the registry are returned; anything else the server
    offers is ignored, so a model never sees an unregistered tool.

    Args:
        server: The server's registry entry.
        connector: Opens the connection.
        exchanger: Obtains the tokens the agent presents, if tokens are used:
            its own token to list the tools, and the caller's on each call.
        pipeline: The agent's tool pipeline.
        bridge: Gives the bridge used by synchronous calls.
        require_pins: Whether every tool must have a schema pin.

    Raises:
        IntegrityError: If a registered tool is not offered, its live input
            schema does not match its pin, or a pin is required and missing.
        PolicyDenied: If the agent cannot get a token of its own for the
            server, or the server does not accept it.
        TransientError: If the server cannot be reached.
    """
    # No user is involved yet: the agent asks in its own name.
    token = (
        await exchanger.service_token(server.audience or server.id)
        if exchanger is not None
        else None
    )
    try:
        async with connector.connect(server, token) as client:
            offered = await _list_tools(client)
    except BaseException as raised:
        error = _unwrap(raised)
        # The connector knows its platform's vocabulary; it is asked first.
        classified = _classified(connector, error, where=f"MCP server {server.id!r}, listing tools")
        if isinstance(classified, PolicyDenied):
            raise classified from None
        if classified is not None:
            raise classified from error
        if _refused_at_the_door(error):
            raise PolicyDenied(
                f"MCP server {server.id!r} did not accept this agent's own token",
                reason_code="server_refused_token",
                expected=f"a token for the server's audience {server.audience or server.id!r}",
                fix=(
                    "check the server's audience in the tool registry against the audience "
                    "its identity provider accepts"
                ),
            ) from None
        if isinstance(error, MCPError | httpx2.HTTPError | OSError | TimeoutError):
            raise TransientError(
                f"MCP server {server.id!r} could not be reached ({describe(error)})",
                expected=f"an MCP server answering at {server.url}",
                fix=(
                    f"start it (in a workspace: agentcraft2 run {server.id}), or correct its "
                    "url in the tool registry"
                ),
            ) from error
        raise
    tools: list[BaseTool] = []
    names: set[str] = set()
    for entry in server.tools:
        # Under the prefix only: an unprefixed offering of the same name is not the tool.
        live = offered.get(server.tool_prefix + entry.name)
        if live is None:
            listed = f" as {server.tool_prefix + entry.name!r}" if server.tool_prefix else ""
            raise IntegrityError(
                f"tool {entry.name!r} is registered for MCP server {server.id!r}, "
                f"but the server does not offer it{listed}"
            )
        if entry.schema_sha256 is None:
            if require_pins:
                raise IntegrityError(
                    f"tool {entry.name!r} of MCP server {server.id!r} has no schema pin"
                )
        elif schema_fingerprint(live.input_schema) != entry.schema_sha256:
            raise IntegrityError(
                f"the input schema of tool {entry.name!r} of MCP server {server.id!r} "
                "does not match its pin in the registry"
            )
        tool = GovernedMcpTool.build(
            server=server,
            entry=entry,
            offered=live,
            connector=connector,
            exchanger=exchanger,
            pipeline=pipeline,
            bridge=bridge,
        )
        if tool.name in names:
            raise IntegrityError(
                f"two tools of MCP server {server.id!r} would have the same name for a "
                f"model: {tool.name!r}"
            )
        names.add(tool.name)
        tools.append(tool)
    return tools
