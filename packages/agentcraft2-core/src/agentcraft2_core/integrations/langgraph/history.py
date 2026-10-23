"""Find the checkpoints one request wrote, under either metadata key."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

__all__ = ["LEGACY_REQUEST_KEY", "REQUEST_KEY", "request_history"]

REQUEST_KEY = "agentcraft2_request_id"
"""The checkpoint metadata key that names the request that wrote the checkpoint."""
LEGACY_REQUEST_KEY = "agentlib_request_id"
"""The key checkpoints were written under before the rename."""


async def request_history(
    graph: Any, config: dict[str, Any], request_id: str, *, limit: int
) -> AsyncIterator[Any]:
    """Yield the graph snapshots recorded for one request, newest first.

    Checkpoints written before the rename carry the old key. It is asked for only
    when nothing was recorded under the current one.

    Args:
        graph: A compiled graph with ``aget_state_history``.
        config: The run configuration, which names the thread.
        request_id: The request to find.
        limit: The most snapshots to read.
    """
    for key in (REQUEST_KEY, LEGACY_REQUEST_KEY):
        found = False
        async for snapshot in graph.aget_state_history(
            config, filter={key: request_id}, limit=limit
        ):
            found = True
            yield snapshot
        if found:
            return
