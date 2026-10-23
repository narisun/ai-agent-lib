"""The Bedrock provider keeps the model provider contract."""

from __future__ import annotations

from botocore import exceptions as aws

from agentcraft2_aws.model_bedrock import BedrockChatModelProvider
from agentcraft2_aws.testing import offline_sessions
from agentcraft2_core.contracts import (
    AgentCraft2Error,
    ChatModelProvider,
    ConfigurationError,
    CredentialsExpiredError,
    TransientError,
)
from agentcraft2_core.testing.langgraph_model_contracts import LangGraphModelProviderContract


def _client_error(code: str, status: int) -> aws.ClientError:
    return aws.ClientError(
        {"Error": {"Code": code, "Message": "m"}, "ResponseMetadata": {"HTTPStatusCode": status}},
        "Converse",
    )


class TestBedrockChatModelProvider(LangGraphModelProviderContract):
    model_id = "anthropic.claude-3-5-sonnet-20240620-v1:0"

    def make_provider(self) -> ChatModelProvider:
        return BedrockChatModelProvider(offline_sessions())

    def vendor_errors(self) -> list[tuple[BaseException, type[AgentCraft2Error]]]:
        return [
            (_client_error("ThrottlingException", 429), TransientError),
            (_client_error("ModelTimeoutException", 408), TransientError),
            (_client_error("ServiceUnavailableException", 503), TransientError),
            (_client_error("AccessDeniedException", 403), ConfigurationError),
            (_client_error("ExpiredTokenException", 403), CredentialsExpiredError),
            (aws.UnauthorizedSSOTokenError(), CredentialsExpiredError),
            (aws.ReadTimeoutError(endpoint_url="https://x"), TransientError),
        ]
