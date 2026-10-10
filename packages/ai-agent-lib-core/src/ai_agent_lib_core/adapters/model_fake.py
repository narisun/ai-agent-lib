"""A scripted chat model for tests and offline development."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from langchain_core.callbacks import AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable
from pydantic import Field, PrivateAttr

from ai_agent_lib_core.contracts import AgentLibError, ModelCapabilities

__all__ = ["FakeChatModel", "FakeChatModelProvider", "ScriptedResponse"]

ScriptedResponse = str | AIMessage | BaseException
"""One scripted turn: text, a full message, or an error to raise."""


def _word_count(text: object) -> int:
    return len(str(text).split())


class FakeChatModel(BaseChatModel):
    """A chat model that replays a script and records what it was asked.

    Each call consumes the next scripted response. A string becomes an
    ``AIMessage``; an ``AIMessage`` is returned as given, so a script can
    include tool calls; an exception is raised. When the script is empty, or
    has run out, the model echoes the last human message.

    Attributes:
        responses: The script.
        model_id: The identifier this instance was created for.
    """

    responses: list[Any] = Field(default_factory=list)
    model_id: str = "fake"

    _calls: list[list[BaseMessage]] = PrivateAttr(default_factory=list)
    _bound_tools: list[Any] = PrivateAttr(default_factory=list)
    _position: int = PrivateAttr(default=0)

    @property
    def _llm_type(self) -> str:
        return "agentlib-fake"

    @property
    def calls(self) -> list[list[BaseMessage]]:
        """The message lists this model has been called with, oldest first."""
        return self._calls

    @property
    def bound_tools(self) -> list[Any]:
        """The tools most recently bound to this model."""
        return self._bound_tools

    def bind_tools(
        self, tools: Sequence[Any], *, tool_choice: str | None = None, **kwargs: Any
    ) -> Runnable[Any, AIMessage]:
        """Remember the tools and return the model itself."""
        self._bound_tools = list(tools)
        return self

    def _next(self, messages: list[BaseMessage]) -> AIMessage:
        self._calls.append(list(messages))
        if self._position < len(self.responses):
            scripted = self.responses[self._position]
            self._position += 1
            if isinstance(scripted, BaseException):
                raise scripted
            message = scripted if isinstance(scripted, AIMessage) else AIMessage(content=scripted)
        else:
            last_human = next(
                (m.content for m in reversed(messages) if isinstance(m, HumanMessage)), ""
            )
            message = AIMessage(content=f"fake: {last_human}")
        if message.usage_metadata is None:
            prompt = sum(_word_count(m.content) for m in messages)
            answer = _word_count(message.content)
            message = message.model_copy(
                update={
                    "usage_metadata": {
                        "input_tokens": prompt,
                        "output_tokens": answer,
                        "total_tokens": prompt + answer,
                    }
                }
            )
        return message

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=self._next(messages))])

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=self._next(messages))])


class FakeChatModelProvider:
    """Provides :class:`FakeChatModel` instances.

    Args:
        responses: A script shared by every model this provider creates, in the
            order the models are called. Without one, models echo their input.
    """

    def __init__(self, responses: Sequence[ScriptedResponse] | None = None) -> None:
        self._responses = list(responses or [])
        self.models: list[FakeChatModel] = []

    @property
    def capabilities(self) -> ModelCapabilities:
        """The fake supports tool calling and nothing vendor-specific."""
        return ModelCapabilities(tool_calling=True, structured_output=True)

    def create(self, model_id: str) -> FakeChatModel:
        """Return a fake model that replays this provider's script."""
        model = FakeChatModel(responses=self._responses, model_id=model_id)
        self.models.append(model)
        return model

    def classify_error(self, error: BaseException) -> AgentLibError | None:
        """Pass library errors through unchanged; recognise nothing else."""
        return error if isinstance(error, AgentLibError) else None
