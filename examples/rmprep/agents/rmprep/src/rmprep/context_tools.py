"""Keep tool arguments aligned with the client and evidence date selected for this turn."""

from collections.abc import Sequence

from langchain_core.messages import AIMessage

from rmprep.memos import MemoVersion
from rmprep.selection import ContextSelection


def tool_context_issues(
    reply: AIMessage,
    selection: ContextSelection,
    memos: Sequence[MemoVersion],
    today: str,
) -> list[str]:
    """Reject a model's client/date drift before any tool executes."""
    selected = [memo for memo in memos if memo["memo_id"] in selection["memo_ids"]]
    clients = {memo["uen"] for memo in selected}
    if selection["uen"]:
        clients.add(selection["uen"])
    issues: list[str] = []
    requested_clients = {
        str(call["args"]["uen"]).upper() for call in reply.tool_calls if "uen" in call["args"]
    }
    if len(requested_clients) > 1 and len(selected) < 2:
        issues.append("Retrieve evidence for one selected client at a time.")
    for call in reply.tool_calls:
        arguments = call["args"]
        uen = str(arguments.get("uen", "")).upper()
        if uen and clients and uen not in clients:
            issues.append("Use only the selected client UEN: " + ", ".join(sorted(clients)) + ".")
        if call["name"].endswith("find_clients"):
            continue
        dates = {memo["meeting_date"] for memo in selected if not uen or memo["uen"] == uen}
        if selection["meeting_date"]:
            dates.add(selection["meeting_date"])
        if selection["mode"] == "refresh":
            dates = {today}
        key = "meeting_date" if call["name"].endswith("get_memo_evidence") else "end_date"
        if dates and str(arguments.get(key, "")) not in dates:
            issues.append(
                f"Set {key} explicitly to the selected evidence date: "
                + ", ".join(sorted(dates))
                + "."
            )
    return issues
