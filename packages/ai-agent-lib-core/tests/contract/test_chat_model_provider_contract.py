"""Every chat model provider passes the same contract suite."""

from __future__ import annotations

import anthropic
import httpx2
from pydantic import SecretStr

from ai_agent_lib_core.adapters import AnthropicChatModelProvider, FakeChatModelProvider
from ai_agent_lib_core.contracts import (
    AgentLibError,
    ChatModelProvider,
    ConfigurationError,
    TransientError,
)
from ai_agent_lib_core.testing.contracts import ChatModelProviderContract


def _status(code: int) -> anthropic.APIStatusError:
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return anthropic.APIStatusError(
        "vendor message", response=httpx2.Response(code, request=request), body=None
    )


class TestAnthropicChatModelProvider(ChatModelProviderContract):
    def make_provider(self) -> ChatModelProvider:
        return AnthropicChatModelProvider(SecretStr("sk-test-key"))

    def vendor_errors(self) -> list[tuple[BaseException, type[AgentLibError]]]:
        return [
            (_status(429), TransientError),
            (_status(503), TransientError),
            (_status(401), ConfigurationError),
        ]


class TestFakeChatModelProvider(ChatModelProviderContract):
    def make_provider(self) -> ChatModelProvider:
        return FakeChatModelProvider()
