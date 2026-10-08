"""The agent side: registered MCP tools as governed LangChain tools."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any, Protocol

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

from ai_agent_lib_core.contracts import (
    AgentLibError,
    IntegrityError,
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
from ai_agent_lib_core.contracts.structured import model_safe_name, schema_mismatch
from ai_agent_lib_core.integrations.langgraph.bridge import LoopBridge
from ai_agent_lib_core.integrations.langgraph.context import current_request_context
from ai_agent_lib_core.integrations.langgraph.tools import GovernedTool
from ai_agent_lib_core.integrations.mcp.wire import (
    META_CLASSIFICATION_CEILING,
    META_REQUEST_ID,
    META_THREAD_ID,
    is_error_result,
    mcp_result_text,
)
from ai_agent_lib_core.pipeline import Pipeline, ToolCall, ToolStage

__all__ = ["GovernedMcpTool", "HttpMcpConnector", "McpConnector", "load_mcp_tools"]

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


class HttpMcpConnector:
    """Connects over Streamable HTTP, with the token in the ``Authorization`` header.

    Args:
        timeout_seconds: How long one HTTP exchange may take.
    """

    def __init__(self, *, timeout_seconds: float = 30.0) -> None:
        self._timeout = timeout_seconds

    @asynccontextmanager
    async def connect(self, server: ServerEntry, token: SecretStr | None) -> AsyncIterator[Client]:
        """Open one HTTP client and one MCP session for the enclosed block."""
        headers = {}
        if token is not None:
            headers["Authorization"] = f"Bearer {token.get_secret_value()}"
        async with (
            httpx2.AsyncClient(
                headers=headers,
                timeout=self._timeout,
                follow_redirects=False,
                # The environment is read by the configuration layer only.
                trust_env=False,
            ) as http,
            Client(streamable_http_client(server.url, http_client=http)) as client,
        ):
            yield client

    def credential_meta(self, token: SecretStr | None) -> Mapping[str, str]:  # noqa: ARG002
        """Return nothing: over HTTP the token is in a header."""
        return {}


_REFUSED_STATUS = frozenset({401, 403})


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


class GovernedMcpTool(GovernedTool):
    """A registered MCP tool, called through the agent's tool pipeline.

    To a model and to a ``ToolNode`` it is an ordinary tool. Each call is
    checked against the registries and the policy, the caller's identity is
    exchanged for a token bound to the server, and the result comes back
    framed as untrusted data.
    """

    _server: ServerEntry = PrivateAttr()
    _mcp_name: str = PrivateAttr()
    _connector: McpConnector = PrivateAttr()
    _exchanger: TokenExchanger | None = PrivateAttr(default=None)
    _validator: Draft202012Validator = PrivateAttr()

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
        tool._mcp_name = entry.name
        tool._connector = connector
        tool._exchanger = exchanger
        tool._validator = Draft202012Validator(offered.input_schema)
        tool._handler = pipeline.bind(tool._call_inner)
        tool._bridge = bridge
        return tool

    def _make_call(self, arguments: dict[str, Any], config: RunnableConfig) -> ToolCall:
        return ToolCall(
            context=current_request_context(),
            tool=self._mcp_name,
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
                f"the arguments for tool {self._mcp_name!r} do not match its input schema",
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
        where = f"MCP server {self._server.id!r}, tool {self._mcp_name!r}"
        try:
            async with self._connector.connect(self._server, token) as client:
                result = await client.call_tool(self._mcp_name, arguments, meta=meta)  # type: ignore[arg-type]
        except BaseException as raised:
            error = _unwrap(raised)
            if isinstance(error, MCPError):
                raise AgentLibError(
                    f"{where}: the server answered with an error",
                    actual=describe(error),
                    fix="look at the server's log line for this request ID",
                ) from None
            if _refused_at_the_door(error):
                # Not a passing fault: the same token would be refused again.
                raise PolicyDenied(
                    f"{where}: the server did not accept the caller's token",
                    reason_code="server_refused_token",
                ) from None
            if isinstance(error, httpx2.HTTPError | OSError | TimeoutError):
                raise TransientError(
                    f"{where}: the server could not be reached ({describe(error)})",
                    expected=f"an MCP server answering at {self._server.url}",
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
                    f"start it (in a workspace: agentlib run {server.id}), or correct its "
                    "url in the tool registry"
                ),
            ) from error
        raise
    tools: list[BaseTool] = []
    names: set[str] = set()
    for entry in server.tools:
        live = offered.get(entry.name)
        if live is None:
            raise IntegrityError(
                f"tool {entry.name!r} is registered for MCP server {server.id!r}, "
                "but the server does not offer it"
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
