"""A reusable model/tool loop for specialists with different instructions."""

import json
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from ai_agent_lib_core import RequestContext, ValidationFailed
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool
from langgraph.graph import START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt import ToolNode, tools_condition

from rmprep.context import ContextBuilder
from rmprep.context_tools import tool_context_issues
from rmprep.memos import MemoVersion, memo_from_turn, turns
from rmprep.response_review import response_issues, tool_payload, word_limit
from rmprep.selection import ContextSelection, select_context
from rmprep.state import State


def _accepted_update(
    state: State,
    reply: AIMessage,
    memo: MemoVersion | None,
    selection: ContextSelection,
    diagnostics: dict[str, object],
) -> dict[str, Any]:
    """Commit a checked answer, its context decision and any new immutable memo together."""
    reply.additional_kwargs["rmprep_context"] = dict(selection)
    update: dict[str, Any] = {
        "messages": [reply],
        "context_diagnostics": diagnostics,
        "selection": selection,
    }
    if not reply.tool_calls:
        update["active_client"] = selection["uen"] or state.get("active_client", "")
        if len(selection["memo_ids"]) == 1:
            update["active_memo_id"] = selection["memo_ids"][0]
        elif selection["uen"] != state.get("active_client", ""):
            update["active_memo_id"] = ""
    if memo is not None:
        update.update(memos=[memo], active_memo_id=memo["memo_id"], active_client=memo["uen"])
    return update


def build_specialist(
    model: Runnable[Any, AIMessage],
    tools: Sequence[BaseTool],
    prompt: str,
    *,
    default_word_limit: int = 350,
    context_builder: ContextBuilder | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> CompiledStateGraph[Any, Any, Any, Any]:
    """Use injected, governed model and tools; inherit the parent checkpoint."""
    contexts = context_builder or ContextBuilder()

    async def call_model(state: State) -> dict[str, Any]:
        current = turns(state["messages"])[-1]
        creating = state.get("route") == "memoagent"
        selection = state.get("selection") or select_context(
            str(current[0].content),
            state.get("memos", []),
            state.get("active_memo_id", ""),
            state.get("active_client", ""),
            creating_memo=creating,
        )
        if selection["clarification"]:
            return {
                "messages": [AIMessage(selection["clarification"])],
                "context_diagnostics": dict(selection),
            }
        # A name lookup can resolve a new client during the tool loop. Only
        # successful MCP payloads establish that identity, never assistant prose.
        clients = {
            str(tool_payload(message)["uen"])
            for message in current
            if "uen" in tool_payload(message) and tool_payload(message).get("status") != "not_found"
        }
        if not selection["uen"] and len(clients) == 1 and not selection["memo_ids"]:
            selection = {**selection, "uen": next(iter(clients))}
        selection = {
            **selection,
            "retrieval_date": now().date().isoformat()
            if selection["mode"] == "refresh"
            else selection["meeting_date"],
        }
        limit = word_limit(state["messages"], default_word_limit)
        # The application adds a stable reference only to evidence-backed memos.
        footer_reserve = (
            12 if creating and any(tool.name.endswith("get_memo_evidence") for tool in tools) else 0
        )
        body_limit = max(1, limit - footer_reserve)
        instructions = (
            f"\nOutput budget: at most {body_limit} whitespace-separated words INCLUDING headings, tables "
            f"and citations. Aim for {int(body_limit * 0.8)} words to leave room for source references. "
            "Copy complete source_ref strings exactly; never shorten them."
        )
        assembled = contexts.build(state, prompt + instructions, selection)
        conversation = list(assembled.messages)
        for attempt in range(3):
            contexts.check_size(conversation)
            reply = await model.ainvoke(conversation)
            reply = reply.model_copy(update={"id": reply.id or str(uuid4())})
            memo = (
                memo_from_turn([*current, reply], state.get("memos", []), now().isoformat())
                if creating
                else None
            )
            if memo is not None:
                reply = reply.model_copy(
                    update={
                        "content": memo["content"]
                        + (
                            f"\n\nMemo reference: {memo['memo_id']} | version {memo['version']} | "
                            f"{memo['uen']} | {memo['meeting_date']}"
                        )
                    }
                )
                memo["content"] = str(reply.content)
            issues = response_issues(reply, assembled.messages[1:], limit)
            issues += tool_context_issues(
                reply, selection, state.get("memos", []), now().date().isoformat()
            )
            if (
                creating
                and footer_reserve
                and not reply.tool_calls
                and memo is None
                and selection["uen"]
                and not any(
                    isinstance(message, ToolMessage)
                    and (message.name or "").endswith("get_memo_evidence")
                    for message in current
                )
            ):
                issues.append(
                    "Retrieve a successful get_memo_evidence pack before writing a memo. If unavailable, explain the missing client/evidence instead."
                )
            if (
                selection["mode"] == "refresh"
                and not reply.tool_calls
                and not any(tool_payload(message) for message in current)
            ):
                issues.append(
                    "Retrieve current evidence before describing what has changed; otherwise state that current evidence is unavailable."
                )
            if not issues:
                return _accepted_update(state, reply, memo, selection, assembled.diagnostics)
            if attempt < 2:
                # Drafts remain visible in model tracing but are never saved as
                # accepted answers in the shared conversation checkpoint.
                conversation.extend(
                    [
                        # Invalid tool calls are never sent as an unmatched call/result
                        # pair to a provider, and never reach the tool node.
                        AIMessage(
                            "Proposed tool calls (not executed): " + json.dumps(reply.tool_calls)
                        )
                        if reply.tool_calls
                        else reply,
                        HumanMessage(
                            "Revise the draft to pass these output checks: "
                            + " ".join(issues)
                            + " Return a corrected tool call or answer. Preserve evidence limits; "
                            "do not add unsupported claims while shortening."
                        ),
                    ]
                )
        raise ValidationFailed(
            "The answer could not meet its length, citation or evidence-limit checks after two revisions."
        )

    builder = StateGraph(State, context_schema=RequestContext)
    builder.add_node("model", call_model)
    builder.add_node("tools", ToolNode(tools))
    builder.add_edge(START, "model")
    builder.add_conditional_edges("model", tools_condition)
    builder.add_edge("tools", "model")
    return builder.compile()
