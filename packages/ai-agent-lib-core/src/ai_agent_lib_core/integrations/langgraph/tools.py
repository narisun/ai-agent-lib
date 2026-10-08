"""Governed tools: native tools whose calls run the tool pipeline."""

from __future__ import annotations

import logging
from collections.abc import Callable, Collection, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Self

from langchain_core.messages import ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool
from langchain_core.tools import tool as as_tool
from langchain_core.utils.pydantic import get_fields
from pydantic import PrivateAttr

from ai_agent_lib_core.integrations.langgraph.bridge import LoopBridge
from ai_agent_lib_core.integrations.langgraph.context import current_request_context
from ai_agent_lib_core.pipeline import (
    Handler,
    Pipeline,
    ToolCall,
    ToolStage,
    bind_request_context,
)

__all__ = ["GovernedTool", "govern_tools"]

_TOOL_CALL = "tool_call"
_LOG = logging.getLogger(__name__)

# The ID of the model's tool call, for the duration of one invocation. LangChain
# hands it to a tool only through an injected argument, so it is kept here for
# the step that calls the wrapped tool.
_TOOL_CALL_ID: ContextVar[str | None] = ContextVar("agentlib_tool_call_id", default=None)


def _tool_call_id(tool_input: object) -> str | None:
    """Return the ID of a model tool call, or ``None`` for plain arguments."""
    if isinstance(tool_input, Mapping) and tool_input.get("type") == _TOOL_CALL:
        call_id = tool_input.get("id")
        return call_id if isinstance(call_id, str) and call_id else None
    return None


def _injected_arguments(tool: BaseTool) -> frozenset[str]:
    """Return the arguments the framework supplies, which a model never writes.

    These are graph state, the store, the runtime and the tool call ID. They
    are what the tool's full input schema has beyond the schema a model is shown.
    """
    full, shown = tool.get_input_schema(), tool.tool_call_schema
    if isinstance(shown, dict) or isinstance(tool.args_schema, dict):
        return frozenset()
    return frozenset(get_fields(full)) - frozenset(get_fields(shown))


@dataclass(slots=True)
class _Invocation:
    """What the wrapped tool is called with, carried as the pipeline payload.

    Attributes:
        config: The run configuration of the call.
        arguments: Every argument, including the ones the framework supplied.
        tool_call_id: The ID of the model's tool call, if there was one.
        reply: The message the wrapped tool answered with, kept so that its
            artifact and status survive while the pipeline works on its content.
    """

    config: RunnableConfig
    arguments: Mapping[str, Any]
    tool_call_id: str | None
    reply: ToolMessage | None = field(default=None)


class GovernedTool(BaseTool):
    """A tool that sends every call through the tool pipeline.

    It has the same name, description and argument schema as the tool it wraps,
    so a model and a ``ToolNode`` see no difference. The native features keep
    working: injected state, store, runtime and tool call ID reach the wrapped
    tool; a ``Command`` it returns is passed back unchanged; and an artifact
    returned with ``response_format="content_and_artifact"`` stays on the
    tool message.

    Policy and guardrails see only the arguments a model supplied. Arguments
    the framework injects, such as graph state, are not tool input. A
    ``Command`` is the tool author's own instruction to the graph, so messages
    inside it are not framed; the audit record says when a result was not.
    """

    read_only: bool = False

    _inner: BaseTool = PrivateAttr()
    _injected: frozenset[str] = PrivateAttr(default=frozenset())
    _handler: Handler[ToolCall, Any] = PrivateAttr()
    _bridge: Callable[[], LoopBridge] = PrivateAttr()

    @classmethod
    def wrap(
        cls,
        inner: BaseTool,
        *,
        pipeline: Pipeline[ToolStage, ToolCall, Any],
        bridge: Callable[[], LoopBridge],
        read_only: bool = False,
    ) -> Self:
        """Return a governed tool that delegates to ``inner``."""
        governed = cls(
            name=inner.name,
            description=inner.description,
            args_schema=inner.args_schema,
            return_direct=inner.return_direct,
            read_only=read_only,
        )
        governed._inner = inner
        governed._injected = _injected_arguments(inner)
        governed._handler = pipeline.bind(governed._call_inner)
        governed._bridge = bridge
        return governed

    # ------------------------------------------------------------ entry points

    def invoke(
        self,
        input: str | dict[str, Any] | Any,  # noqa: A002 - the base class names it
        config: RunnableConfig | None = None,
        **kwargs: Any,
    ) -> Any:
        """Run the tool, remembering the ID of the model's tool call."""
        token = _TOOL_CALL_ID.set(_tool_call_id(input))
        try:
            return super().invoke(input, config, **kwargs)
        finally:
            _TOOL_CALL_ID.reset(token)

    async def ainvoke(
        self,
        input: str | dict[str, Any] | Any,  # noqa: A002 - the base class names it
        config: RunnableConfig | None = None,
        **kwargs: Any,
    ) -> Any:
        """Run the tool, remembering the ID of the model's tool call."""
        token = _TOOL_CALL_ID.set(_tool_call_id(input))
        try:
            return await super().ainvoke(input, config, **kwargs)
        finally:
            _TOOL_CALL_ID.reset(token)

    # ------------------------------------------------------------------- calls

    def _make_call(self, arguments: dict[str, Any], config: RunnableConfig) -> ToolCall:
        # The context is read here, on the caller's side, before any thread hop.
        return ToolCall(
            context=current_request_context(),
            tool=self.name,
            # Only what a model wrote is tool input; the rest travels in the payload.
            arguments={k: v for k, v in arguments.items() if k not in self._injected},
            read_only=self.read_only,
            payload=_Invocation(
                config=config, arguments=arguments, tool_call_id=_TOOL_CALL_ID.get()
            ),
        )

    async def _call_inner(self, call: ToolCall) -> Any:
        if call.context is None:
            return await self._run_inner(call)
        # The tool runs under the context the pipeline passed on, which a stage may
        # have narrowed: the registry lowers the classification ceiling to the agent's.
        with bind_request_context(call.context):
            return await self._run_inner(call)

    async def _run_inner(self, call: ToolCall) -> Any:
        invocation = call.payload
        if not isinstance(invocation, _Invocation):
            raise TypeError("the tool call payload was replaced inside the pipeline")
        arguments = dict(invocation.arguments)
        try:
            if invocation.tool_call_id is None:
                return await self._inner.ainvoke(arguments, invocation.config)
            reply = await self._inner.ainvoke(
                {
                    "name": self._inner.name,
                    "args": arguments,
                    "id": invocation.tool_call_id,
                    "type": _TOOL_CALL,
                },
                invocation.config,
            )
        except Exception as error:
            # The tool's own code raised. The framework usually turns this into a
            # message for the model, so without this line the developer would
            # never see it: its type, where it was raised and the line of the
            # tool's code are logged; its message only with logging details on.
            _LOG.warning(
                "the tool %s raised %s",
                self.name,
                type(error).__name__,
                exc_info=error,
                extra={"tool": self.name},
            )
            raise
        if isinstance(reply, ToolMessage):
            # The pipeline checks and frames the content; the message is rebuilt after it.
            invocation.reply = reply
            if reply.status == "error":
                call.evidence.add(tool_error=True)
            return reply.content
        # A Command or another control object of the framework: passed back as it is.
        return reply

    @staticmethod
    def _released(call: ToolCall, result: Any) -> Any:
        """Put what the pipeline released back on the wrapped tool's own message."""
        invocation = call.payload
        reply = invocation.reply if isinstance(invocation, _Invocation) else None
        if reply is None or not isinstance(result, str | list):
            return result
        return reply.model_copy(update={"content": result})

    def _parse_input(
        self,
        tool_input: str | dict[str, Any],
        tool_call_id: str | None,  # noqa: ARG002 - the base class names it
    ) -> str | dict[str, Any]:
        """Pass the input on as it came: the wrapped tool validates it, inside the pipeline.

        LangChain validates here, before ``_arun``. A malformed call would then
        never reach the pipeline, so no policy decision or audit record would
        show it. The wrapped tool applies the same schema, with the same error.
        """
        return dict(tool_input) if isinstance(tool_input, dict) else tool_input

    def _keywords(self, args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, Any]:
        """Name a single plain input, as LangChain does for a tool with one argument."""
        if not args:
            return kwargs
        names = [name for name in self._inner.args if name not in self._injected]
        if len(args) == 1 and not kwargs and names:
            return {names[0]: args[0]}
        raise TypeError(f"tool {self.name!r} takes its arguments by name: {', '.join(names)}")

    async def _arun(self, *args: Any, config: RunnableConfig, **kwargs: Any) -> Any:
        call = self._make_call(self._keywords(args, kwargs), config)
        return self._released(call, await self._handler(call))

    def _run(self, *args: Any, config: RunnableConfig, **kwargs: Any) -> Any:
        call = self._make_call(self._keywords(args, kwargs), config)
        return self._released(call, self._bridge().run(lambda: self._handler(call)))


def govern_tools(
    tools: Sequence[BaseTool | Callable[..., Any]],
    *,
    pipeline: Pipeline[ToolStage, ToolCall, Any],
    bridge: Callable[[], LoopBridge],
    read_only: Collection[str] = (),
) -> list[BaseTool]:
    """Wrap each tool, converting plain functions to tools first.

    Args:
        tools: Tools, or functions with type hints and a docstring.
        pipeline: The tool pipeline.
        bridge: Gives the bridge used by synchronous calls.
        read_only: Names of tools that only read. Only these may be retried.

    Raises:
        ValueError: If two tools share a name, a ``read_only`` name is unknown,
            or a tool is already governed.
    """
    governed: list[BaseTool] = []
    names: set[str] = set()
    for item in tools:
        if isinstance(item, GovernedTool):
            raise ValueError(f"tool {item.name!r} is already governed")
        inner = item if isinstance(item, BaseTool) else as_tool(item)
        if inner.name in names:
            raise ValueError(f"two tools are named {inner.name!r}")
        names.add(inner.name)
        governed.append(
            GovernedTool.wrap(
                inner, pipeline=pipeline, bridge=bridge, read_only=inner.name in read_only
            )
        )
    unknown = sorted(set(read_only) - names)
    if unknown:
        raise ValueError(f"read_only names tools that were not given: {unknown}")
    return governed
