"""The model type required by the LangGraph integration, outside core contracts."""

from typing import Protocol

from langchain_core.language_models import BaseChatModel

from ai_agent_lib_core.contracts import ChatModelProvider

__all__ = ["LangGraphModelProvider", "create_chat_model"]


class LangGraphModelProvider(ChatModelProvider, Protocol):
    """A provider whose model implements LangChain's native chat model interface."""

    def create(self, model_id: str) -> BaseChatModel:
        """Return a native chat model supporting the provider's declared capabilities."""
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
