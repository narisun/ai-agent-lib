"""Find the checkpoints one request wrote, under either metadata key."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import aclosing
from typing import Any

__all__ = ["LEGACY_REQUEST_KEY", "REQUEST_KEY", "request_history"]

REQUEST_KEY = "agentcraft2_request_id"
"""The checkpoint metadata key that names the request that wrote the checkpoint."""
LEGACY_REQUEST_KEY = "agentlib_request_id"
"""The key checkpoints were written under before the rename."""


async def request_history(
    graph: Any, config: dict[str, Any], request_id: str, *, limit: int
) -> AsyncGenerator[Any, None]:
    """Yield the graph snapshots recorded for one request, newest first.

    Checkpoints written before the rename carry the old key. It is asked for only
    when nothing was recorded under the current one. Closing this iterator, or
    abandoning it part way, closes the graph's own iterator at once: it holds a
    database cursor, and Python 3.13 releases an abandoned one later than 3.11 did.

    Args:
        graph: A compiled graph with ``aget_state_history``.
        config: The run configuration, which names the thread.
        request_id: The request to find.
        limit: The most snapshots to read.
    """
    for key in (REQUEST_KEY, LEGACY_REQUEST_KEY):
        found = False
        async with aclosing(
            graph.aget_state_history(config, filter={key: request_id}, limit=limit)
        ) as snapshots:
            async for snapshot in snapshots:
                found = True
                yield snapshot
        if found:
            return
