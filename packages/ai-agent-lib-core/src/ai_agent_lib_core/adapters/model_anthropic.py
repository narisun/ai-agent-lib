"""The Anthropic API model provider.

The ``langchain-anthropic`` package is an optional dependency; install the
``anthropic`` extra of ``ai-agent-lib-core`` to use this provider.

Note on TLS: ``langchain-anthropic`` builds its own HTTP client and offers no
option for a CA file, so this provider cannot pass one explicitly. Behind a
TLS-inspecting proxy the CA file has to reach that client through the standard
variables of the underlying HTTP library.
"""

from __future__ import annotations

from typing import Any

from pydantic import SecretStr

from ai_agent_lib_core.contracts import (
    AgentLibError,
    ConfigurationError,
    ModelCapabilities,
    TransientError,
)

__all__ = ["AnthropicChatModelProvider"]

_INSTALL_HINT = "install it with: pip install 'ai-agent-lib-core[anthropic]'"
_TRANSIENT_STATUS = frozenset({408, 409, 429})
_REJECTED_STATUS = frozenset({401, 403})
_SERVER_ERROR = 500


class AnthropicChatModelProvider:
    """Builds chat models served by the Anthropic API.

    Args:
        api_key: The API key.
        proxy: Optional outbound proxy URL.
        timeout_seconds: Optional request timeout.
        max_retries: Retries done by the vendor client. Set this to ``0`` once
            the pipeline's own resilience stage is in use, so retries are not
            multiplied.

    Raises:
        ConfigurationError: If the optional dependency is missing or the key is empty.
    """

    def __init__(
        self,
        api_key: SecretStr,
        *,
        proxy: str | None = None,
        timeout_seconds: float | None = None,
        max_retries: int = 2,
    ) -> None:
        try:
            import anthropic
            from langchain_anthropic import ChatAnthropic
        except ImportError as exc:
            raise ConfigurationError(
                f"the anthropic model provider is not installed; {_INSTALL_HINT}"
            ) from exc
        if not api_key.get_secret_value():
            raise ConfigurationError("the anthropic model provider needs a non-empty API key")
        self._anthropic = anthropic
        self._chat_model = ChatAnthropic
        self._api_key = api_key
        self._proxy = proxy
        self._timeout = timeout_seconds
        self._max_retries = max_retries

    @property
    def capabilities(self) -> ModelCapabilities:
        """What the Anthropic API offers through this provider."""
        return ModelCapabilities(
            tool_calling=True,
            structured_output=True,
            prompt_caching=True,
            token_counting=True,
        )

    def create(self, model_id: str) -> object:
        """Return a ``ChatAnthropic`` model for ``model_id``."""
        options: dict[str, Any] = {
            "model": model_id,
            "api_key": self._api_key,
            "max_retries": self._max_retries,
        }
        if self._proxy is not None:
            options["anthropic_proxy"] = self._proxy
        if self._timeout is not None:
            options["timeout"] = self._timeout
        return self._chat_model(**options)

    def classify_error(self, error: BaseException) -> AgentLibError | None:
        """Map Anthropic SDK errors to the taxonomy, by HTTP status.

        The vendor's message is not copied into the new error, because it can
        repeat parts of the request.
        """
        sdk = self._anthropic
        name = type(error).__name__
        if isinstance(error, sdk.APIStatusError):
            status = error.status_code
            if status in _TRANSIENT_STATUS or status >= _SERVER_ERROR:
                return TransientError(f"the anthropic API returned HTTP {status} ({name})")
            if status in _REJECTED_STATUS:
                return ConfigurationError(
                    f"the anthropic API rejected the credentials with HTTP {status} ({name})"
                )
            return None
        if isinstance(error, sdk.APIConnectionError):
            return TransientError(f"the anthropic API could not be reached ({name})")
        return None
