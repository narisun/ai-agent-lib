"""Project accepted memo state into optional UI artifacts without importing the UI."""

from __future__ import annotations

from typing import Any

from ai_agent_lib_core import RequestContext

from rmprep.state import State


def memo_artifacts(state: State, context: RequestContext) -> list[dict[str, Any]]:
    """Publish only a memo produced by this turn, never an older memo carried into Q&A."""
    if state.get("route") != "memoagent" or not state.get("messages"):
        return []
    message = state["messages"][-1]
    for memo in state.get("memos", []):
        if (
            memo["memo_id"] == state.get("active_memo_id")
            and str(message.id) in memo["message_ids"]
            and memo["content"] == str(message.text)
        ):
            return [
                {
                    "schema": "agentlib.artifact/v1",
                    "id": memo["memo_id"],
                    "document_id": "rm-memo-" + memo["uen"],
                    "version": memo["version"],
                    "title": f"{memo['client_name']} · {memo['meeting_date']}",
                    "media_type": "text/markdown",
                    "created_at": memo["generated_at"],
                    "request_id": context.request_id,
                    "thread_id": context.thread_id,
                    "content": memo["content"],
                }
            ]
    return []
