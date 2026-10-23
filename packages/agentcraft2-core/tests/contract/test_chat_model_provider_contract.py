"""Every chat model provider passes the same contract suite."""

from __future__ import annotations

import anthropic
import httpx2
from pydantic import SecretStr

from agentcraft2_core.adapters import AnthropicChatModelProvider, FakeChatModelProvider
from agentcraft2_core.contracts import (
    AgentCraft2Error,
    ChatModelProvider,
    ConfigurationError,
    TransientError,
)
from agentcraft2_core.testing.langgraph_model_contracts import LangGraphModelProviderContract


def _status(code: int) -> anthropic.APIStatusError:
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return anthropic.APIStatusError(
        "vendor message", response=httpx2.Response(code, request=request), body=None
    )


class TestAnthropicChatModelProvider(LangGraphModelProviderContract):
    def make_provider(self) -> ChatModelProvider:
        return AnthropicChatModelProvider(SecretStr("sk-test-key"))

    def vendor_errors(self) -> list[tuple[BaseException, type[AgentCraft2Error]]]:
        return [
            (_status(429), TransientError),
            (_status(503), TransientError),
            (_status(401), ConfigurationError),
        ]


class TestFakeChatModelProvider(LangGraphModelProviderContract):
    def make_provider(self) -> ChatModelProvider:
        return FakeChatModelProvider()
