"""The governed chat model: a native chat model whose calls run the model pipeline."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Self

from langchain_core.callbacks import AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable, RunnableLambda
from langchain_core.tools import BaseTool
from pydantic import BaseModel, PrivateAttr

from ai_agent_lib_core.contracts import (
    AuditValue,
    ChatModelProvider,
    ConfigurationError,
    ModelRef,
    StructuredOutput,
    ValidationFailed,
)
from ai_agent_lib_core.integrations.langgraph.bridge import LoopBridge
from ai_agent_lib_core.integrations.langgraph.context import current_request_context
from ai_agent_lib_core.integrations.langgraph.providers import create_chat_model
from ai_agent_lib_core.pipeline import Handler, ModelCall, ModelStage, Pipeline

__all__ = [
    "GovernedChatModel",
    "describe_model_response",
    "message_text",
    "messages_text",
    "structured_payload",
]


@dataclass(frozen=True, slots=True)
class _Invocation:
    """Everything the provider call needs, carried as the pipeline payload."""

    messages: list[BaseMessage]
    stop: list[str] | None
    kwargs: Mapping[str, Any] = field(default_factory=dict)


def describe_model_response(message: AIMessage) -> Mapping[str, AuditValue]:
    """Return audit metadata for a model reply: token counts and tool-call count."""
    described: dict[str, AuditValue] = {"tool_calls": len(message.tool_calls)}
    usage = message.usage_metadata
    if usage is not None:
        described["input_tokens"] = usage.get("input_tokens")
        described["output_tokens"] = usage.get("output_tokens")
    return described


def message_text(message: BaseMessage) -> str:
    """Return the text of one message, including the arguments of any tool calls."""
    content = message.content
    parts: list[str] = []
    if isinstance(content, str):
        parts.append(content)
    else:
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block.get("text"), str):
                parts.append(block["text"])
    if isinstance(message, AIMessage):
        parts.extend(
            json.dumps(call["args"], default=str, ensure_ascii=False) for call in message.tool_calls
        )
    return "\n".join(part for part in parts if part)


def messages_text(messages: Sequence[BaseMessage]) -> str:
    """Return the text of every message, for the input guardrails."""
    return "\n".join(message_text(message) for message in messages)


def structured_payload(message: AIMessage, name: str) -> object:
    """Return what a reply holds for the schema called ``name``.

    A model asked for structured output normally answers with one call to the
    schema's tool, whose arguments are the value. Failing that, the reply text
    is returned, to be parsed as strict JSON.

    Raises:
        ValidationFailed: If the reply calls the schema more than once.
    """
    calls = [call for call in message.tool_calls if call["name"] == name]
    if len(calls) > 1:
        raise ValidationFailed(
            "the model produced more than one structured output",
            expected=f"one call of the tool {name!r}",
            actual=f"{len(calls)} calls of it",
            fix="ask for a single answer in the instructions",
        )
    if calls:
        return calls[0]["args"]
    return message_text(message)


def _tool_name(tool: object) -> str | None:
    if isinstance(tool, BaseTool):
        return tool.name
    if isinstance(tool, Mapping):
        function = tool.get("function")
        named = function if isinstance(function, Mapping) else tool
        name = named.get("name")
        return name if isinstance(name, str) else None
    return getattr(tool, "__name__", None)


def _origin(tool: object) -> str:
    server = getattr(tool, "_server", None)
    server_id = getattr(server, "id", None)
    return f"MCP server {server_id!r}" if server_id else "this agent's own tools"


def _require_distinct_names(tools: Sequence[object]) -> None:
    seen: dict[str, object] = {}
    for tool in tools:
        name = _tool_name(tool)
        if name is None:
            continue
        if name in seen:
            raise ConfigurationError(
                f"two tools offered to the model have the name {name!r}",
                expected="a distinct name for every tool the model may call",
                actual=f"{name!r} from {_origin(seen[name])} and from {_origin(tool)}",
                fix=(
                    "register MCP tools under names that start with their server ID, "
                    "such as 'accounts.search', or rename the agent's own tool"
                ),
            )
        seen[name] = tool


def _record_usage(call: ModelCall, reply: AIMessage) -> None:
    """Add what the provider charged to the call's evidence, as soon as it answers.

    A stage on the way back may still reject the reply (an output guardrail, a
    schema check), but the tokens were spent: the budget and the audit record
    count them either way. Retries add up.
    """
    usage = reply.usage_metadata
    if usage is None:
        return
    facts = call.evidence.facts()
    for key in ("input_tokens", "output_tokens"):
        value = usage.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            earlier = facts.get(key)
            total = value + (earlier if isinstance(earlier, int) else 0)
            call.evidence.add(**{key: total})


class GovernedChatModel(BaseChatModel):
    """A chat model that sends every call through the model pipeline.

    It is an ordinary ``BaseChatModel``, so it can be used anywhere LangGraph
    or LangChain expects one. The request context is taken from the graph
    runtime or an explicit binding, never from the messages.

    Attributes:
        alias: The logical model name the application asked for.
        provider_name: The provider that serves the alias.
        model_id: The provider's identifier for the model.
    """

    alias: str
    provider_name: str
    model_id: str
    # LangChain's own cache answers before the model's code runs, so a hit would
    # skip identity, policy, guardrails and audit. A governed model never uses it.
    cache: Literal[False] = False

    _output: StructuredOutput | None = PrivateAttr(default=None)
    _inner: BaseChatModel = PrivateAttr()
    _target: Runnable[Any, Any] = PrivateAttr()
    _provider: ChatModelProvider = PrivateAttr()
    _pipeline: Pipeline[ModelStage, ModelCall, AIMessage] = PrivateAttr()
    _handler: Handler[ModelCall, AIMessage] = PrivateAttr()
    _bridge: Callable[[], LoopBridge] = PrivateAttr()

    @classmethod
    def wrap(
        cls,
        *,
        alias: str,
        ref: ModelRef,
        provider: ChatModelProvider,
        pipeline: Pipeline[ModelStage, ModelCall, AIMessage],
        bridge: Callable[[], LoopBridge],
        target: Runnable[Any, Any] | None = None,
        inner: BaseChatModel | None = None,
        output: StructuredOutput | None = None,
    ) -> Self:
        """Build a governed model for ``alias``.

        Raises:
            TypeError: If the provider does not return a LangChain chat model.
        """
        if inner is None:
            inner = create_chat_model(provider, ref.model_id)
        # The provider's model would consult a process-wide LangChain cache too, and
        # share one caller's answer with another across tenants. Caching, if it is
        # wanted, belongs to a governed stage that scopes and audits it.
        inner.cache = False
        model = cls(alias=alias, provider_name=ref.provider, model_id=ref.model_id)
        model._inner = inner
        model._target = target if target is not None else inner
        model._provider = provider
        model._pipeline = pipeline
        model._handler = pipeline.bind(model._call_provider)
        model._bridge = bridge
        model._output = output
        return model

    @property
    def _llm_type(self) -> str:
        return "agentlib-governed"

    @property
    def _identifying_params(self) -> dict[str, Any]:
        return {"alias": self.alias, "provider": self.provider_name, "model_id": self.model_id}

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> Runnable[Any, AIMessage]:
        """Bind tools on the underlying model and keep the result governed.

        Raises:
            ConfigurationError: If two tools have the same name. The model calls a
                tool by name and a ``ToolNode`` keeps one tool per name, so the
                second would silently take the first one's calls.
        """
        if not self._provider.capabilities.tool_calling:
            raise ConfigurationError(
                f"model provider {self.provider_name!r} does not support tools"
            )
        _require_distinct_names(tools)
        if tool_choice is not None:
            kwargs["tool_choice"] = tool_choice
        bound = self._inner.bind_tools(tools, **kwargs)
        return type(self).wrap(
            alias=self.alias,
            ref=ModelRef(provider=self.provider_name, model_id=self.model_id),
            provider=self._provider,
            pipeline=self._pipeline,
            bridge=self._bridge,
            target=bound,
            inner=self._inner,
        )

    def with_structured_output(
        self,
        schema: StructuredOutput | Mapping[str, Any] | type[BaseModel],
        *,
        include_raw: bool = False,
        **kwargs: Any,
    ) -> Runnable[Any, Any]:
        """Return a runnable whose reply is validated against ``schema``.

        The model is asked to answer by calling one tool whose parameters are
        the schema. The reply is validated inside the model pipeline, so a
        reply that does not match fails the call and is recorded as failed.

        Args:
            schema: A :class:`StructuredOutput`, a JSON Schema or a Pydantic model.
            include_raw: Return ``{"raw": message, "parsed": value}`` instead
                of the value alone.
            **kwargs: Passed to the underlying model when the tool is bound.

        Returns:
            A runnable that gives the validated value: an instance of the
            Pydantic model when one was given, otherwise a dictionary.
        """
        capabilities = self._provider.capabilities
        if not capabilities.structured_output or not capabilities.tool_calling:
            raise ConfigurationError(
                f"model provider {self.provider_name!r} does not support structured output "
                "through tool calling"
            )
        output = schema if isinstance(schema, StructuredOutput) else StructuredOutput(schema)
        kwargs.setdefault("tool_choice", "any")
        bound = self._inner.bind_tools([output.tool_definition()], **kwargs)
        governed = type(self).wrap(
            alias=self.alias,
            ref=ModelRef(provider=self.provider_name, model_id=self.model_id),
            provider=self._provider,
            pipeline=self._pipeline,
            bridge=self._bridge,
            target=bound,
            inner=self._inner,
            output=output,
        )

        def finish(message: AIMessage) -> Any:
            # Already validated in the pipeline; this only converts the value.
            payload = structured_payload(message, output.name)
            parsed = output.parse(payload) if isinstance(payload, str) else output.validate(payload)
            return {"raw": message, "parsed": parsed} if include_raw else parsed

        return governed | RunnableLambda(finish)

    # ----------------------------------------------------------------- calls

    def _make_call(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None,
        kwargs: Mapping[str, Any],
    ) -> ModelCall:
        # The context is read here, on the caller's side, before any thread hop.
        return ModelCall(
            context=current_request_context(),
            alias=self.alias,
            provider=self.provider_name,
            model_id=self.model_id,
            payload=_Invocation(messages=messages, stop=stop, kwargs=kwargs),
            text=messages_text(messages),
            output=self._output,
        )

    async def _call_provider(self, call: ModelCall) -> AIMessage:
        invocation = call.payload
        if not isinstance(invocation, _Invocation):
            raise TypeError("the model call payload was replaced inside the pipeline")
        extra = dict(invocation.kwargs)
        if invocation.stop is not None:
            extra["stop"] = invocation.stop
        try:
            # No config is passed: the underlying call inherits callbacks and
            # tracing from the surrounding run through LangChain's own context.
            reply = await self._target.ainvoke(invocation.messages, **extra)
        except Exception as error:
            mapped = self._provider.classify_error(error)
            if mapped is None or mapped is error:
                raise
            raise mapped from error
        if not isinstance(reply, AIMessage):
            raise TypeError(f"the model returned {type(reply).__name__}, not an AIMessage")
        _record_usage(call, reply)
        return reply

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        reply = await self._handler(self._make_call(messages, stop, kwargs))
        return ChatResult(generations=[ChatGeneration(message=reply)])

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        call = self._make_call(messages, stop, kwargs)
        reply = self._bridge().run(lambda: self._handler(call))
        return ChatResult(generations=[ChatGeneration(message=reply)])
