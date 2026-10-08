"""The Amazon Bedrock model provider.

The ``langchain-aws`` package is an optional dependency; install the
``bedrock`` extra of ``ai-agent-lib-aws`` to use this provider.

A developer reaches Bedrock with an SSO profile and, behind a TLS-inspecting
proxy, the enterprise CA file. Both come from configuration; nothing else
changes between the Anthropic API and Bedrock.
"""

from __future__ import annotations

from ai_agent_lib_aws.session import AwsSessionFactory
from ai_agent_lib_core.contracts import AgentLibError, ConfigurationError, ModelCapabilities

__all__ = ["BedrockChatModelProvider"]

_INSTALL_HINT = "install it with: pip install 'ai-agent-lib-aws[bedrock]'"
_WHAT = "the bedrock model provider"


class BedrockChatModelProvider:
    """Builds chat models served by Amazon Bedrock through the Converse API.

    The SDK clients are injected into every model, so a model never builds a
    client of its own from the process environment.

    Args:
        sessions: Builds the Bedrock clients.

    Raises:
        ConfigurationError: If the optional dependency is missing, or the
            clients cannot be built.
    """

    def __init__(self, sessions: AwsSessionFactory) -> None:
        try:
            from langchain_aws import ChatBedrockConverse
        except ImportError as exc:
            raise ConfigurationError(f"{_WHAT} is not installed; {_INSTALL_HINT}") from exc
        self._chat_model = ChatBedrockConverse
        self._sessions = sessions
        # The model pipeline's resilience stage owns retries; one SDK attempt each.
        self._runtime = sessions.client("bedrock-runtime", retried_by_pipeline=True)
        # The control plane client is used only to resolve an inference profile to its model.
        self._control = sessions.client("bedrock")

    def __repr__(self) -> str:
        return f"BedrockChatModelProvider(region={self._sessions.region!r})"

    @property
    def capabilities(self) -> ModelCapabilities:
        """What Bedrock offers through the Converse API."""
        return ModelCapabilities(
            tool_calling=True,
            structured_output=True,
            prompt_caching=True,
            token_counting=True,
        )

    def create(self, model_id: str) -> object:
        """Return a ``ChatBedrockConverse`` model for a model or inference-profile ID."""
        return self._chat_model(
            model_id=model_id, client=self._runtime, bedrock_client=self._control
        )

    def classify_error(self, error: BaseException) -> AgentLibError | None:
        """Map AWS SDK errors to the taxonomy.

        An expired SSO sign-in becomes ``CredentialsExpiredError`` and names
        the command that renews it. The SDK's message is not copied.
        """
        return self._sessions.classify(error, what=_WHAT)
