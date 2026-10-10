"""Executable model conformance at the LangChain boundary, with scripted SDK replies.

Subclass this suite instead of the framework-neutral model suite for LangGraph
providers. Transport-specific response decoding still belongs in adapter tests.
Only the SDK's generation step is substituted: native invocation, tool binding,
governance, structured parsing and error propagation execute normally.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import BaseModel

from ai_agent_lib_core.contracts import ModelSection, Principal, RequestContext, ServiceConfig
from ai_agent_lib_core.di import ServiceContainer
from ai_agent_lib_core.integrations.langgraph import GovernedChatModel
from ai_agent_lib_core.integrations.langgraph.providers import create_chat_model
from ai_agent_lib_core.pipeline import bind_request_context
from ai_agent_lib_core.testing.contracts import ChatModelProviderContract
from ai_agent_lib_core.testing.harness import Fakes

__all__ = ["LangGraphModelProviderContract"]


class _Answer(BaseModel):
    value: str


class LangGraphModelProviderContract(ChatModelProviderContract):
    """A model provider must work through the framework, beyond constructing an object."""

    model_id = "any-model-id"

    @asynccontextmanager
    async def scripted(
        self,
        monkeypatch: pytest.MonkeyPatch,
        reply: AIMessage | Exception,
    ) -> AsyncIterator[GovernedChatModel]:
        """Substitute only the SDK's network generation operation."""
        provider = self.make_provider()
        raw = create_chat_model(provider, self.model_id)

        async def generate(*args: Any, **kwargs: Any) -> ChatResult:
            if isinstance(reply, Exception):
                raise reply
            return ChatResult(generations=[ChatGeneration(message=reply)])

        monkeypatch.setattr(type(raw), "_agenerate", generate)
        providers = Fakes().providers()
        providers.register("model", "fake", lambda _context: provider, replace=True)
        async with ServiceContainer(
            ServiceConfig.for_testing(model=ModelSection(provider="fake", model_id=self.model_id)),
            providers,
        ) as services:
            context = RequestContext(Principal("reader", "tenant"), "agent", "r", "thread")
            with bind_request_context(context):
                yield services.model()

    async def test_native_async_invocation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async with self.scripted(monkeypatch, AIMessage("answer")) as model:
            reply = await model.ainvoke([HumanMessage("question")])
            assert reply.content == "answer"

    async def test_native_tool_binding(self, monkeypatch: pytest.MonkeyPatch) -> None:
        if not self.make_provider().capabilities.tool_calling:
            pytest.skip("provider declares no tool calling")

        def lookup(value: str) -> str:
            """Look up a value."""
            return value

        response = AIMessage("", tool_calls=[{"name": "lookup", "args": {"value": "a"}, "id": "1"}])
        async with self.scripted(monkeypatch, response) as model:
            reply = await model.bind_tools([lookup]).ainvoke([HumanMessage("lookup a")])
            assert reply.tool_calls[0]["args"] == {"value": "a"}

    async def test_native_structured_output(self, monkeypatch: pytest.MonkeyPatch) -> None:
        if not self.make_provider().capabilities.structured_output:
            pytest.skip("provider declares no structured output")
        response = AIMessage(
            "", tool_calls=[{"name": "_Answer", "args": {"value": "a"}, "id": "1"}]
        )
        async with self.scripted(monkeypatch, response) as model:
            answer = await model.with_structured_output(_Answer).ainvoke([HumanMessage("answer")])
            assert answer == _Answer(value="a")

    async def test_unclassified_errors_propagate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        failure = ValueError("adapter defect")
        async with self.scripted(monkeypatch, failure) as model:
            with pytest.raises(ValueError, match="adapter defect") as caught:
                await model.ainvoke([HumanMessage("question")])
            assert caught.value is failure
