"""The LangGraph bindings: the four touchpoints a graph author uses."""

from __future__ import annotations

from collections.abc import Callable, Collection, Sequence
from typing import TYPE_CHECKING, Any

from langchain_core.messages import AIMessage
from langchain_core.tools import BaseTool
from langgraph.checkpoint.base import BaseCheckpointSaver

from ai_agent_lib_core.contracts import (
    DEFAULT_MODEL_ALIAS,
    AuditSink,
    ChatModelProvider,
    CheckpointBackend,
    Clock,
    ConfigurationError,
    GuardrailCheck,
    IdGenerator,
    ModelRef,
    PolicyDecisionPoint,
    RegistrySource,
    RequestContext,
    ServerEntry,
    Telemetry,
    TokenExchanger,
)
from ai_agent_lib_core.integrations.langgraph.bridge import LoopBridge
from ai_agent_lib_core.integrations.langgraph.checkpoint import (
    ScopedCheckpointer,
    scoped_thread_id,
)
from ai_agent_lib_core.integrations.langgraph.model import (
    GovernedChatModel,
    describe_model_response,
    message_text,
    structured_payload,
)
from ai_agent_lib_core.integrations.langgraph.tools import govern_tools
from ai_agent_lib_core.pipeline import (
    ModelCall,
    ModelStage,
    Pipeline,
    ToolCall,
    ToolStage,
    build_model_pipeline,
    build_tool_pipeline,
)

if TYPE_CHECKING:
    from ai_agent_lib_core.integrations.mcp.client import McpConnector

__all__ = ["LangGraphBindings"]


class LangGraphBindings:
    """Hands out governed, native LangGraph objects.

    Args:
        audit: The audit port.
        telemetry: The telemetry port.
        clock: The clock port.
        ids: The identifier port.
        resolve_model: Returns the concrete model and provider behind an alias.
        checkpoint_backend: The store for graph thread state.
        registry: The agent and tool registries.
        policy: The policy decision point.
        environment: Where the process is running, as the policy is told.
        guardrails: The guardrail port.
        frame_tool_results: Whether tool results are framed as untrusted data.
        bridge: Gives the bridge used by synchronous calls.
    """

    def __init__(
        self,
        *,
        audit: AuditSink,
        telemetry: Telemetry,
        clock: Clock,
        ids: IdGenerator,
        resolve_model: Callable[[str], tuple[ModelRef, ChatModelProvider]],
        checkpoint_backend: CheckpointBackend,
        registry: RegistrySource,
        policy: PolicyDecisionPoint,
        environment: str,
        guardrails: GuardrailCheck,
        frame_tool_results: bool,
        bridge: Callable[[], LoopBridge],
    ) -> None:
        self._resolve_model = resolve_model
        self._checkpoint_backend = checkpoint_backend
        self._bridge = bridge
        self._model_pipeline: Pipeline[ModelStage, ModelCall, AIMessage] = build_model_pipeline(
            audit=audit,
            telemetry=telemetry,
            clock=clock,
            ids=ids,
            describe_response=describe_model_response,
            policy=policy,
            environment=environment,
            guardrails=guardrails,
            response_text=message_text,
            structured_payload=structured_payload,
        )
        self._tool_pipeline: Pipeline[ToolStage, ToolCall, Any] = build_tool_pipeline(
            audit=audit,
            telemetry=telemetry,
            clock=clock,
            ids=ids,
            registry=registry,
            policy=policy,
            environment=environment,
            guardrails=guardrails,
            frame_results=frame_tool_results,
        )

    def model(self, alias: str = DEFAULT_MODEL_ALIAS) -> GovernedChatModel:
        """Return the governed chat model behind ``alias``. It is a ``BaseChatModel``."""
        ref, provider = self._resolve_model(alias)
        return GovernedChatModel.wrap(
            alias=alias,
            ref=ref,
            provider=provider,
            pipeline=self._model_pipeline,
            bridge=self._bridge,
        )

    def tools(
        self,
        tools: Sequence[BaseTool | Callable[..., Any]],
        *,
        read_only: Collection[str] = (),
    ) -> list[BaseTool]:
        """Return governed versions of ``tools``."""
        return govern_tools(
            tools, pipeline=self._tool_pipeline, bridge=self._bridge, read_only=read_only
        )

    async def mcp_tools(
        self,
        server: ServerEntry,
        *,
        connector: McpConnector | None,
        exchanger: TokenExchanger | None,
        require_pins: bool,
    ) -> list[BaseTool]:
        """Return the registered tools of an MCP server, governed by the tool pipeline."""
        # Imported here so that an agent which uses no MCP server does not load the SDK.
        from ai_agent_lib_core.integrations.mcp.client import HttpMcpConnector, load_mcp_tools

        return await load_mcp_tools(
            server=server,
            connector=connector if connector is not None else HttpMcpConnector(),
            exchanger=exchanger,
            pipeline=self._tool_pipeline,
            bridge=self._bridge,
            require_pins=require_pins,
        )

    def compile_kwargs(self) -> dict[str, Any]:
        """Return keyword arguments for ``StateGraph.compile``: the scoped checkpointer."""
        checkpointer = self._checkpoint_backend.checkpointer
        if not isinstance(checkpointer, BaseCheckpointSaver):
            raise ConfigurationError(
                "the checkpoint backend did not provide a LangGraph checkpointer"
            )
        return {"checkpointer": ScopedCheckpointer(checkpointer)}

    def invocation(self, context: RequestContext) -> dict[str, Any]:
        """Return keyword arguments for ``ainvoke`` or ``astream`` for one request.

        The thread ID is scoped to the caller, and the request context travels
        as runtime context, outside graph state.
        """
        if not isinstance(context, RequestContext):
            raise TypeError("context must be a RequestContext")
        thread_id = scoped_thread_id(context.scope, context.thread_id)
        return {"config": {"configurable": {"thread_id": thread_id}}, "context": context}
