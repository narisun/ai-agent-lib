"""The checkpoint provider for a service that keeps no graph state."""

from __future__ import annotations

from ai_agent_lib_core.contracts import ConfigurationError

__all__ = ["NoCheckpointBackend"]


class NoCheckpointBackend:
    """Stands where a checkpoint store would be, and holds nothing.

    An MCP server compiles no graph, so it has no thread state to keep and
    should not need a database for it. Selecting this provider says so. Asking
    it for a checkpointer is a configuration mistake and is reported as one.
    """

    @property
    def checkpointer(self) -> object:
        """Refuse: there is no checkpointer.

        Raises:
            ConfigurationError: Always.
        """
        raise ConfigurationError(
            "this service has no checkpoint store: its checkpoint provider is 'none'. "
            "Select a checkpoint provider to compile a graph that keeps thread state"
        )
