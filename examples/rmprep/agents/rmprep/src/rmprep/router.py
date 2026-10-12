"""Small, deterministic routing rules for the first business example."""

import re

from rmprep.memos import index_legacy_memos
from rmprep.selection import select_context
from rmprep.state import Route, State


def choose_route(question: str) -> Route:
    """Recognize a memo command; send other questions to the Q&A specialist.

    Prefixes make the choice explicit. The starter also recognizes requests
    beginning with a memo creation verb. Broader intent classification can
    replace this function without changing the specialist graphs.
    """
    text = question.strip().casefold()
    if text.startswith("qna:"):
        return "qnaagent"
    if text.startswith("memo:"):
        return "memoagent"
    if re.search(
        r"(?:^|[,.;]\s+)(?:(?:please|could you|can you|would you|i need you to)\s+)*"
        r"(?:prepare|create|generate|refresh|update|write)\b[^.!?\n]*\bmemo\b",
        text,
    ):
        return "memoagent"
    return "qnaagent"


def router(state: State) -> dict[str, object]:
    """Route using the newest human question, never a tool result or old memo."""
    question = next(message for message in reversed(state["messages"]) if message.type == "human")
    route = choose_route(str(question.content))
    memos = state.get("memos", [])
    migrated = state.get("context_version") != 1 and not memos
    if migrated:
        memos = index_legacy_memos(
            state["messages"], lambda text: choose_route(text) == "memoagent"
        )
    active_id = state.get("active_memo_id", memos[-1]["memo_id"] if memos else "")
    active_client = state.get("active_client", memos[-1]["uen"] if memos else "")
    return {
        "route": route,
        "context_version": 1,
        **(
            {"memos": memos, "active_memo_id": active_id, "active_client": active_client}
            if migrated
            else {}
        ),
        "selection": select_context(
            str(question.content),
            memos,
            active_id,
            active_client,
            creating_memo=route == "memoagent",
        ),
    }


def destination(state: State) -> Route:
    """Read the decision made by the router node."""
    return state["route"]
