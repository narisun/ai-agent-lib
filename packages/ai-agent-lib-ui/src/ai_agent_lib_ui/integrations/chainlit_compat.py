"""Compatibility boundary for the pinned Chainlit 2.12 chat shell.

Its synchronous session.get_config tries to run an async profile callback on
the running socket loop. That fails and leaks a coroutine. Agent Lab profiles
select registry agents; they all use the same shell configuration, with no
per-profile overrides. Resolve that shared configuration synchronously while
leaving the authenticated async profile catalog unchanged.
"""

from typing import Any


def use_shared_profile_config() -> None:
    """Install the shared-config path before creating any WebSocket sessions."""
    from chainlit.config import config
    from chainlit.session import WebsocketSession

    def shared_config(session: Any) -> Any:
        cached = getattr(session, "config", None)
        return cached if cached is not None else config

    # Keep this deliberate framework method replacement at one typed boundary.
    session_class: Any = WebsocketSession
    session_class.get_config = shared_config
