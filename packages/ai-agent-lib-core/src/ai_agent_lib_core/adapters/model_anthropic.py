"""The Anthropic API model provider.

The ``langchain-anthropic`` package is an optional dependency; install the
``anthropic`` extra of ``ai-agent-lib-core`` to use this provider.

Note on TLS: ``langchain-anthropic`` builds its own HTTP client and offers no
option for a CA file, so this provider cannot pass one explicitly. Behind a
TLS-inspecting proxy the CA file has to reach that client through the standard
variables of the underlying HTTP library.
"""

from __future__ import annotations

from typing import Any, Literal

from langchain_core.messages import BaseMessage, SystemMessage
from pydantic import Field, SecretStr

from ai_agent_lib_core.contracts import (
    AgentLibError,
    ConfigurationError,
    ModelCapabilities,
    OptionsModel,
    TransientError,
)

__all__ = ["AnthropicChatModelProvider", "AnthropicModelOptions"]

_INSTALL_HINT = "install it with: pip install 'ai-agent-lib-core[anthropic]'"
_TRANSIENT_STATUS = frozenset({408, 409, 429})
_REJECTED_STATUS = frozenset({401, 403})
_SERVER_ERROR = 500


class AnthropicModelOptions(OptionsModel):
    """Anthropic prompt caching; every call still runs the model pipeline.

    Set these under ``anthropic`` in the model options map. Requires the
    ``anthropic`` extra and secret ``anthropic_api_key`` (``ANTHROPIC_API_KEY``
    locally, Secrets Manager when deployed). Uses the configured HTTPS proxy.
    """

    prompt_caching: bool = Field(
        default=False, strict=True, description="Enable automatic Anthropic prompt caching."
    )
    cache_ttl: Literal["5m", "1h"] = Field(
        default="5m",
        description="Cache lifetime. One-hour writes cost more than five-minute writes.",
    )


class AnthropicChatModelProvider:
    """Builds chat models served by the Anthropic API.

    Args:
        api_key: The API key.
        proxy: Optional outbound proxy URL.
        timeout_seconds: Optional request timeout.
        max_retries: Retries done by the vendor client. Set this to ``0`` once
            the pipeline's own resilience stage is in use, so retries are not
            multiplied.
        options: Prompt caching settings. Disabled by default for existing callers.

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
        options: AnthropicModelOptions | None = None,
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
        self._options = options or AnthropicModelOptions()

    def _cache_control(self) -> dict[str, str] | None:
        if not self._options.prompt_caching:
            return None
        return {"type": "ephemeral", "ttl": self._options.cache_ttl}

    def prepare_request(
        self, messages: list[BaseMessage], options: dict[str, Any]
    ) -> tuple[list[BaseMessage], dict[str, Any]]:
        """Mark the static prefix and enable automatic caching of growing conversations.

        A static_system_prompt marks its first block as stable. The explicit
        breakpoint preserves that prefix when the following request context
        changes. Copies keep cache directives out of the durable checkpoint.
        """
        prepared_options = dict(options)
        control = prepared_options.get("cache_control", self._cache_control())
        if not control:
            return list(messages), prepared_options
        prepared_options["cache_control"] = dict(control)
        prepared = []
        marked = False
        for message in messages:
            if (
                not marked
                and isinstance(message, SystemMessage)
                and message.additional_kwargs.get("agentlib_static_prefix") is True
                and isinstance(message.content, list)
                and message.content
                and isinstance(message.content[0], dict)
                and message.content[0].get("type") == "text"
            ):
                prefix = {**message.content[0], "cache_control": dict(control)}
                message = message.model_copy(update={"content": [prefix, *message.content[1:]]})
                marked = True
            prepared.append(message)
        return prepared, prepared_options

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
        if self._options.prompt_caching:
            options["model_kwargs"] = {"cache_control": self._cache_control()}
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
