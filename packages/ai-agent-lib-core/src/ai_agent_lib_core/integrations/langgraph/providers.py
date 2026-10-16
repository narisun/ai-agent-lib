"""The model type required by the LangGraph integration, outside core contracts."""

from typing import Any, Protocol, runtime_checkable

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage

from ai_agent_lib_core.contracts import ChatModelProvider

__all__ = ["LangGraphModelProvider", "create_chat_model"]


class LangGraphModelProvider(ChatModelProvider, Protocol):
    """A provider whose model implements LangChain's native chat model interface."""

    def create(self, model_id: str) -> BaseChatModel:
        """Return a native chat model supporting the provider's declared capabilities."""
        ...


@runtime_checkable
class ModelRequestPreparer(Protocol):
    """Optional provider hook for request formatting before tracing and dispatch.

    Implementations must preserve message text, return copies, and perform no
    I/O. Existing providers need not implement this hook.
    """

    def prepare_request(
        self, messages: list[BaseMessage], options: dict[str, Any]
    ) -> tuple[list[BaseMessage], dict[str, Any]]:
        """Apply provider-specific transport settings to an already governed request."""
        ...


def create_chat_model(provider: ChatModelProvider, model_id: str) -> BaseChatModel:
    """Create a model and check that it implements the native LangChain type.

    The framework-neutral port returns ``object``. This boundary narrows it to
    ``BaseChatModel``; provider contract tests check invocation, tool binding,
    and other declared capabilities beyond simple type membership.

    Raises:
        TypeError: If the provider returns a different framework's model type.
    """
    model = provider.create(model_id)
    if not isinstance(model, BaseChatModel):
        raise TypeError(
            f"model provider returned {type(model).__name__}, not a LangChain chat model"
        )
    return model
