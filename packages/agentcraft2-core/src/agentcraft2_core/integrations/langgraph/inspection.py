"""Project decoded graph snapshots into bounded, request-linked diagnostic text."""

from collections.abc import Callable, Mapping
from contextlib import aclosing
from typing import Any

from langchain_core.messages import BaseMessage

from agentcraft2_core.contracts import RequestContext
from agentcraft2_core.contracts.inspection import InspectionSection
from agentcraft2_core.integrations.langgraph.history import request_history
from agentcraft2_core.observability.inspection import preview
from agentcraft2_core.pipeline import bind_request_context


def _decoded(value: object) -> object:
    if isinstance(value, BaseMessage):
        return _decoded(value.model_dump())
    if isinstance(value, Mapping):
        return {str(key): _decoded(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_decoded(item) for item in value]
    if value is None or isinstance(value, str | bool | int | float):
        return value
    return f"<{type(value).__name__}>"


class GraphCheckpointPreview:
    """Read through the agent's scoped graph API, never from UI database access.

    Only checkpoints with explicit request metadata are eligible. Older unlinked
    checkpoints are not guessed from timestamps or the last conversation turn.
    """

    def __init__(
        self, graph: Callable[[], Any], invocation: Callable[[RequestContext], dict[str, Any]]
    ) -> None:
        self.graph, self.invocation = graph, invocation

    async def __call__(self, context: RequestContext, request_id: str) -> InspectionSection:
        """Return up to 50 parent graph snapshots for exactly this request."""
        records: list[object] = []
        with bind_request_context(context):
            async with aclosing(
                request_history(
                    self.graph(), self.invocation(context)["config"], request_id, limit=51
                )
            ) as history:
                async for snapshot in history:
                    if len(records) == 50:
                        return preview(records, truncated=True)
                    records.append(
                        {
                            "checkpoint_id": snapshot.config.get("configurable", {}).get(
                                "checkpoint_id"
                            ),
                            "parent_id": (snapshot.parent_config or {})
                            .get("configurable", {})
                            .get("checkpoint_id"),
                            "created_at": snapshot.created_at,
                            "next": snapshot.next,
                            "metadata": _decoded(snapshot.metadata),
                            "values": _decoded(snapshot.values),
                        }
                    )
                    # Stop reading large histories once the preview budget is consumed.
                    if preview(records).truncated:
                        return preview(records, truncated=True)
        return preview(records)
