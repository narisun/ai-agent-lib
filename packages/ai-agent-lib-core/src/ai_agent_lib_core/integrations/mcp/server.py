"""The server side: every tool call an MCP server receives runs the tool pipeline."""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.context import CallNext, HandlerResult, ServerRequestContext
from mcp.server.mcpserver import MCPServer
from mcp_types import CallToolResult, TextContent
from opentelemetry import propagate, trace
from pydantic import SecretStr

from ai_agent_lib_core.contracts import (
    AgentLibError,
    AgentRegistry,
    AuditSink,
    Classification,
    Clock,
    ConfigurationError,
    IdentityVerifier,
    IdGenerator,
    IntegrityError,
    PolicyDenied,
    RequestContext,
    ServerEntry,
    Telemetry,
    TokenAuthenticator,
    ValidationFailed,
    schema_fingerprint,
)
from ai_agent_lib_core.integrations.mcp.wire import (
    META_AUTHORIZATION,
    META_CLASSIFICATION_CEILING,
    META_REQUEST_ID,
    META_THREAD_ID,
    is_error_result,
    mcp_result_text,
)
from ai_agent_lib_core.pipeline import (
    Evidence,
    Pipeline,
    ToolCall,
    ToolStage,
    allowed_agent,
    bind_request_context,
    record_refused_sign_in,
)

__all__ = [
    "DoorTokenVerifier",
    "GovernedToolsMiddleware",
    "resource_server_settings",
    "tool_fingerprints",
    "verify_registration",
]

_TOOLS_CALL = "tools/call"
_BEARER = "bearer "
_MAX_ID_LENGTH = 128

_LOG = logging.getLogger(__name__)
_INSTRUMENTATION = "ai_agent_lib_core"
# A tool is named by the caller. Only a name shaped like a registered one is
# written to a log or a span; anything else could be content.
_TOOL_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_UNNAMED = "unnamed"
_SUCCESS, _TOOL_ERROR, _DENIED, _INVALID, _FAILED, _ERROR = (
    "success",
    "tool_error",
    "denied",
    "invalid",
    "failed",
    "error",
)
_LEVELS = {
    _TOOL_ERROR: logging.WARNING,
    _INVALID: logging.WARNING,
    _FAILED: logging.ERROR,
    _ERROR: logging.ERROR,
}
"""A refusal is the caller's business; a tool that raised, or a failure, is the service's."""


@dataclass(frozen=True, slots=True)
class _Pending:
    """The MCP request a tool call belongs to, carried as the pipeline payload."""

    request: ServerRequestContext[Any, Any]
    call_next: CallNext


def _error(text: str) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=text)], is_error=True)


def _identifier(value: object) -> str | None:
    """Accept a correlation ID from the caller only if it is a plain, short string."""
    if isinstance(value, str) and 0 < len(value) <= _MAX_ID_LENGTH and value.isprintable():
        return value.strip() or None
    return None


def _credential(request: ServerRequestContext[Any, Any]) -> str | None:
    headers = getattr(request.request, "headers", None)
    if headers is not None:
        # Over HTTP only the Authorization header counts.
        value = headers.get("authorization")
        if isinstance(value, str) and value[: len(_BEARER)].lower() == _BEARER:
            return value[len(_BEARER) :].strip() or None
        return None
    value = (request.meta or {}).get(META_AUTHORIZATION)
    return value if isinstance(value, str) and value else None


class DoorTokenVerifier:
    """Checks the bearer token of every HTTP request an MCP server receives.

    This is the MCP SDK's token verifier, backed by the library's identity
    port. The SDK answers a request that carries no valid token with HTTP 401
    and the standard pointer to the server's protected resource metadata, so
    nothing on the server, not even its list of tools, is reachable without one.

    The door asks only whether the token is valid for this server, whoever it
    names. An agent's own token passes, which is how an agent lists tools
    before any user is involved. Whether a caller may call a tool is decided
    afterwards, for each call, by the middleware and the policy.

    Args:
        identity: Tells a valid token from an invalid one.
        server: The server's entry in the tool registry.
        application: The server's application name, for the audit record of a refusal.
        agents: The agent registry.
        require_agent: Whether the token must come from a registered agent that
            is listed for this server. On outside local development.
        audit: Where a refused token is recorded.
        telemetry: The telemetry port.
        clock: The clock port.
        ids: The identifier port.
    """

    def __init__(
        self,
        *,
        identity: TokenAuthenticator,
        server: ServerEntry,
        application: str,
        agents: AgentRegistry,
        require_agent: bool,
        audit: AuditSink,
        telemetry: Telemetry,
        clock: Clock,
        ids: IdGenerator,
    ) -> None:
        self._identity = identity
        self._server = server
        self._application = application
        self._agents = agents
        self._require_agent = require_agent
        self._audit = audit
        self._telemetry = telemetry
        self._clock = clock
        self._ids = ids

    async def verify_token(self, token: str) -> AccessToken | None:
        """Return the access the token gives, or ``None`` to have the request refused.

        Raises:
            IntegrityError: If a refusal could not be written to the audit log.
        """
        try:
            principal = await self._identity.authenticate(token)
            client = principal.actor
            if self._require_agent:
                if client is None:
                    raise PolicyDenied(
                        "the token does not name the application it was issued to",
                        reason_code="agent_unidentified",
                    )
                allowed_agent(self._agents.resolve(client), client, self._server)
        except PolicyDenied as refusal:
            request_id = self._ids.new_id()
            await record_refused_sign_in(
                self._audit,
                self._telemetry,
                self._clock,
                self._ids,
                refusal=refusal,
                application=self._application,
                request_id=request_id,
                thread_id=request_id,
            )
            return None
        return AccessToken(
            token=token,
            client_id=client if client is not None else principal.subject,
            scopes=[],
            subject=principal.subject,
            claims={"iss": self._identity.issuer},
        )


def resource_server_settings(issuer: str, server: ServerEntry) -> AuthSettings:
    """Return the SDK settings that make an MCP server an OAuth resource server.

    Raises:
        ConfigurationError: If the issuer or the server's registered address
            is not a URL the SDK accepts.
    """
    try:
        return AuthSettings(
            issuer_url=issuer,
            resource_server_url=server.url,
            # The identity verifier checks the token's audience itself.
            validate_token_resource=False,
        )
    except ValueError:
        raise ConfigurationError(
            f"MCP server {server.id!r}: the token issuer and the server's registered address "
            "must both be http(s) URLs for the server to check tokens at its door"
        ) from None


@dataclass(frozen=True, slots=True)
class _Ended:
    """How one tool call ended, for its log line."""

    outcome: str
    error: AgentLibError | None = None
    detail: str | None = None


class GovernedToolsMiddleware:
    """Runs every ``tools/call`` request through the tool pipeline.

    The middleware verifies the caller's credential, builds the request
    context, and lets the call reach the tool only if the registry and the
    policy allow it. While the tool runs, the request context is bound, so a
    governed data source used inside the tool knows who is asking, and through
    which agent. Everything else an MCP server receives passes through untouched.

    A refusal is returned to the caller as a tool error that names the reason
    code and nothing else.

    Each call is one ``mcp.tool_call`` span and one ``tool_call`` log line with
    the tool's name, how the call ended and how long it took. Neither holds an
    argument or a result.

    Args:
        server: The server's ID in the tool registry.
        application: The server's application name, as policy and audit see it.
        identity: Verifies the caller's credential.
        pipeline: The tool pipeline.
        ids: Gives a request an ID when the caller did not send one.
        classification_ceiling: The most sensitive data this server may handle.
            A caller can ask for a lower ceiling, never a higher one.
    """

    def __init__(
        self,
        *,
        server: str,
        application: str,
        identity: IdentityVerifier,
        pipeline: Pipeline[ToolStage, ToolCall, HandlerResult],
        ids: IdGenerator,
        classification_ceiling: Classification = Classification.RESTRICTED,
    ) -> None:
        self._server = server
        self._application = application
        self._identity = identity
        self._ids = ids
        self._ceiling = Classification(classification_ceiling)
        self._handler = pipeline.bind(self._call_tool)

    async def __call__(
        self, ctx: ServerRequestContext[Any, Any], call_next: CallNext
    ) -> HandlerResult:
        """Govern a tool call; pass every other request on."""
        if ctx.method != _TOOLS_CALL:
            return await call_next(ctx)
        params = ctx.params or {}
        name, arguments = params.get("name"), params.get("arguments") or {}
        if (
            ctx.request_id is None
            or not isinstance(name, str)
            or not isinstance(arguments, Mapping)
        ):
            # A tool call reaches a tool only through the pipeline. One that is not
            # well formed is refused here and never handed on.
            return _error("The call was refused: it is not a well-formed tool call.")
        evidence = Evidence()
        call = ToolCall(
            context=await self._request_context(ctx, evidence),
            tool=name,
            arguments=arguments,
            server=self._server,
            payload=_Pending(ctx, call_next),
            evidence=evidence,
        )
        return await self._observed(call, ctx.meta)

    async def _observed(self, call: ToolCall, meta: Any = None) -> HandlerResult:
        """Run one call inside a span, and log one line about how it ended.

        The span continues the caller's trace when the call carries W3C trace
        context in its metadata, as the library's own MCP client sends it.
        """
        tool = call.tool if _TOOL_NAME.match(call.tool) else _UNNAMED
        request_id = call.context.request_id if call.context is not None else None
        attributes = {"agentlib.application": self._application, "agentlib.tool": tool}
        if request_id is not None:
            attributes["agentlib.request_id"] = request_id
        started = time.perf_counter()
        ended = _Ended(_ERROR)  # what stands if the tool raises something of its own
        try:
            tracer = trace.get_tracer(_INSTRUMENTATION)
            parent = propagate.extract(meta) if isinstance(meta, Mapping) else None
            with tracer.start_as_current_span(
                "mcp.tool_call",
                context=parent,
                kind=trace.SpanKind.SERVER,
                attributes=attributes,
            ):
                result, ended = await self._answer(call)
                return result
        finally:
            extra: dict[str, object] = {
                "tool": tool,
                "outcome": ended.outcome,
                "duration_ms": round((time.perf_counter() - started) * 1000),
                "request_id": request_id,
            }
            if isinstance(ended.error, PolicyDenied):
                extra["reason_code"] = ended.error.reason_code
            if ended.detail is not None:
                # What the tool's own code raised. It may hold caller content, so
                # it is written only where logging is set up with details.
                extra["detail"] = ended.detail
            _LOG.log(
                _LEVELS.get(ended.outcome, logging.INFO),
                "tool_call",
                exc_info=ended.error,
                extra=extra,
            )

    async def _answer(self, call: ToolCall) -> tuple[HandlerResult, _Ended]:
        """Run the pipeline; return what the caller is told and how the call ended."""
        try:
            result = await self._handler(call)
        except PolicyDenied as denied:
            reply = _error(f"The call was denied (reason: {denied.reason_code}).")
            return reply, _Ended(_DENIED, denied)
        except ValidationFailed as invalid:
            return _error(f"The call was refused: {invalid.summary}."), _Ended(_INVALID, invalid)
        except AgentLibError as error:
            reply = _error(f"The call failed ({type(error).__name__}).")
            return reply, _Ended(_FAILED, error)
        if is_error_result(result):
            return result, _Ended(_TOOL_ERROR, detail=mcp_result_text(result))
        return result, _Ended(_SUCCESS)

    async def _request_context(
        self, request: ServerRequestContext[Any, Any], evidence: Evidence
    ) -> RequestContext | None:
        credential = _credential(request)
        try:
            principal = await self._identity.verify(credential)
        except PolicyDenied as denied:
            # Without a context the pipeline refuses the call and records the refusal.
            evidence.add(identity_reason_code=denied.reason_code)
            return None
        meta = request.meta or {}
        request_id = _identifier(meta.get(META_REQUEST_ID)) or self._ids.new_id()
        ceiling = self._ceiling
        asked = meta.get(META_CLASSIFICATION_CEILING)
        if isinstance(asked, str) and asked.upper() in Classification.__members__:
            ceiling = min(ceiling, Classification[asked.upper()])
        return RequestContext(
            principal=principal,
            application=self._application,
            request_id=request_id,
            thread_id=_identifier(meta.get(META_THREAD_ID)) or request_id,
            classification_ceiling=ceiling,
            credential=SecretStr(credential) if credential else None,
        )

    @staticmethod
    async def _call_tool(call: ToolCall) -> HandlerResult:
        pending = call.payload
        if not isinstance(pending, _Pending) or call.context is None:
            raise TypeError("the tool call was replaced inside the pipeline")
        with bind_request_context(call.context):
            result = await pending.call_next(pending.request)
        if is_error_result(result):
            call.evidence.add(tool_error=True)
        return result


async def tool_fingerprints(server: MCPServer[Any]) -> dict[str, str]:
    """Return the schema fingerprint of every tool a server offers, by tool name.

    These are the values to pin as ``schema_sha256`` in the tool registry.
    """
    return {tool.name: schema_fingerprint(tool.input_schema) for tool in await server.list_tools()}


async def verify_registration(
    server: MCPServer[Any], entry: ServerEntry, *, require_pins: bool = True
) -> None:
    """Check at startup that a server offers exactly what the registry says.

    Raises:
        IntegrityError: If a registered tool is missing, a tool's input schema
            does not match its pin, a tool is not pinned and pins are required,
            or the server offers a tool that is not registered.
    """
    offered = await tool_fingerprints(server)
    registered = {tool.name: tool for tool in entry.tools}
    problems = [
        f"{name} is registered but not offered" for name in sorted(set(registered) - set(offered))
    ]
    problems += [
        f"{name} is offered but not registered" for name in sorted(set(offered) - set(registered))
    ]
    for name in sorted(set(offered) & set(registered)):
        pin = registered[name].schema_sha256
        if pin is None and require_pins:
            problems.append(f"{name} has no schema pin")
        elif pin is not None and pin != offered[name]:
            problems.append(f"the input schema of {name} does not match its pin")
    if problems:
        raise IntegrityError(
            f"MCP server {entry.id!r} does not match the tool registry: " + "; ".join(problems)
        )
