"""Durable conversation focus, independent of the action requested on each turn."""

from __future__ import annotations

from typing import TYPE_CHECKING, TypedDict

if TYPE_CHECKING:
    from rmprep.state import State


class ConversationFocus(TypedDict):
    """The accepted subject, document and evidence date for subsequent questions."""

    client_uens: list[str]
    memo_ids: list[str]
    evidence_date: str
    message_ids: list[str]


def conversation_focus(state: State) -> ConversationFocus:
    """Normalize older checkpoints once at the routing boundary, without rewriting history."""
    if "focus" in state:
        return state["focus"]
    memos = state.get("memos", [])
    active_id = state.get("active_memo_id", memos[-1]["memo_id"] if memos else "")
    client = state.get("active_client", memos[-1]["uen"] if memos else "")
    selected = next((memo for memo in memos if memo["memo_id"] == active_id), None)
    # Older answers already carry accepted context metadata. A newer Q&A refresh
    # takes precedence over the legacy active_memo_id that used to remain stale.
    for message in reversed(state["messages"]):
        metadata = message.additional_kwargs.get("rmprep_context", {})
        if message.type == "ai" and metadata and not getattr(message, "tool_calls", []):
            if metadata.get("uen") == client and metadata.get("retrieval_date"):
                date = metadata["retrieval_date"]
                if selected is None or date != selected["meeting_date"]:
                    return ConversationFocus(
                        client_uens=metadata.get("client_uens") or ([client] if client else []),
                        memo_ids=[],
                        evidence_date=date,
                        message_ids=[],
                    )
            break
    return ConversationFocus(
        client_uens=state.get("active_client_uens") or ([client] if client else []),
        memo_ids=[selected["memo_id"]] if selected else [],
        evidence_date=selected["meeting_date"] if selected else "",
        message_ids=[],
    )
