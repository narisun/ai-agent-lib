"""The wheel consumer gate runs this suite outside the source checkout."""

import os

from agentcraft2_core.contracts import ChatModelProvider
from agentcraft2_core.testing.langgraph_model_contracts import LangGraphModelProviderContract


class TestInstalledModel(LangGraphModelProviderContract):
    """Exercise the selected installed provider without making a network call."""

    model_id = "anthropic.claude-3-5-sonnet-20240620-v1:0"

    def make_provider(self) -> ChatModelProvider:
        """Build the selected provider using local test credentials."""
        provider = os.environ["AGENTCRAFT2_CONSUMER_MODEL"]
        if provider == "fake":
            from agentcraft2_core.adapters import FakeChatModelProvider

            return FakeChatModelProvider()
        if provider == "anthropic":
            from pydantic import SecretStr

            from agentcraft2_core.adapters import AnthropicChatModelProvider

            return AnthropicChatModelProvider(SecretStr("test"))
        if provider == "bedrock":
            from agentcraft2_aws.model_bedrock import BedrockChatModelProvider
            from agentcraft2_aws.testing import offline_sessions

            return BedrockChatModelProvider(offline_sessions())
        raise AssertionError(f"unknown consumer model {provider}")
