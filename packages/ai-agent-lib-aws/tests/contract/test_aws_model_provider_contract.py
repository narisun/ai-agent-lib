"""The Bedrock provider keeps the model provider contract."""

from __future__ import annotations

from botocore import exceptions as aws

from ai_agent_lib_aws.model_bedrock import BedrockChatModelProvider
from ai_agent_lib_aws.testing import offline_sessions
from ai_agent_lib_core.contracts import (
    AgentLibError,
    ChatModelProvider,
    ConfigurationError,
    CredentialsExpiredError,
    TransientError,
)
from ai_agent_lib_core.testing.contracts import ChatModelProviderContract


def _client_error(code: str, status: int) -> aws.ClientError:
    return aws.ClientError(
        {"Error": {"Code": code, "Message": "m"}, "ResponseMetadata": {"HTTPStatusCode": status}},
        "Converse",
    )


class TestBedrockChatModelProvider(ChatModelProviderContract):
    def make_provider(self) -> ChatModelProvider:
        return BedrockChatModelProvider(offline_sessions())

    def vendor_errors(self) -> list[tuple[BaseException, type[AgentLibError]]]:
        return [
            (_client_error("ThrottlingException", 429), TransientError),
            (_client_error("ModelTimeoutException", 408), TransientError),
            (_client_error("ServiceUnavailableException", 503), TransientError),
            (_client_error("AccessDeniedException", 403), ConfigurationError),
            (_client_error("ExpiredTokenException", 403), CredentialsExpiredError),
            (aws.UnauthorizedSSOTokenError(), CredentialsExpiredError),
            (aws.ReadTimeoutError(endpoint_url="https://x"), TransientError),
        ]
