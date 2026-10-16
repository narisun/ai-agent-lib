"""Immutable memo versions stored alongside the conversation in its scoped checkpoint."""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from hashlib import sha256
from typing import TypedDict

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage

from rmprep.response_review import evidence_references, text_content, tool_payload

_MEMO_FOOTER = re.compile(
    r"^Memo reference: memo-[a-z0-9]{12}\s*\|\s*version \d+\s*\|\s*"
    r"[^|\n]+\s*\|\s*\d{4}-\d{2}-\d{2}\s*$",
    re.MULTILINE,
)


def memo_draft(reply: AIMessage) -> AIMessage:
    """Remove copied application footers before assigning this answer its own identity."""
    if reply.tool_calls:
        return reply
    return reply.model_copy(update={"content": _MEMO_FOOTER.sub("", text_content(reply)).rstrip()})


class MemoVersion(TypedDict):
    """Identify an accepted memo and the exact conversation that produced it."""

    memo_id: str
    version: int
    uen: str
    client_name: str
    meeting_date: str
    generated_at: str
    content: str
    message_ids: list[str]
    source_refs: list[str]


def merge_memos(existing: list[MemoVersion], added: list[MemoVersion]) -> list[MemoVersion]:
    """Merge parent/subgraph updates without allowing an existing version to change."""
    indexed = {memo["memo_id"]: memo for memo in existing}
    for memo in added:
        previous = indexed.get(memo["memo_id"])
        if previous is not None and previous != memo:
            raise ValueError("An accepted memo version cannot be changed")
        indexed[memo["memo_id"]] = memo
    return list(indexed.values())


def turns(messages: Sequence[AnyMessage]) -> list[list[AnyMessage]]:
    """Group whole user/model/tool exchanges; never split a tool call from its result."""
    groups: list[list[AnyMessage]] = []
    for message in messages:
        if isinstance(message, HumanMessage) or not groups:
            groups.append([])
        groups[-1].append(message)
    return groups


def memo_from_turn(
    messages: Sequence[AnyMessage],
    previous: Sequence[MemoVersion],
    generated_at: str,
) -> MemoVersion | None:
    """Register only an accepted answer backed by one successful memo evidence pack.

    Message IDs point to the original evidence in the append-only conversation.
    A new version copies its content and provenance; refreshing never edits an old one.
    """
    if not messages or not isinstance(messages[-1], AIMessage) or messages[-1].tool_calls:
        return None
    packs = [tool_payload(message) for message in messages]
    packs = [pack for pack in packs if pack.get("status") == "found" and "meeting_date" in pack]
    identities = {(pack["uen"], pack["meeting_date"]) for pack in packs}
    if len(identities) != 1:
        return None
    pack = packs[-1]
    uen = str(pack["uen"])
    profile = pack.get("sections", {}).get("profile", {}).get("records", [])
    name = str(profile[0].get("client_name", profile[0].get("name", uen))) if profile else uen
    # The accepted answer's ID is stable across checkpoint restoration and graph replay.
    memo_id = "memo-" + sha256(str(messages[-1].id).encode("utf-8")).hexdigest()[:12]
    return MemoVersion(
        memo_id=memo_id,
        version=1 + sum(memo["uen"] == uen for memo in previous),
        uen=uen,
        client_name=name,
        meeting_date=str(pack["meeting_date"]),
        generated_at=generated_at,
        content=text_content(messages[-1]),
        message_ids=[str(message.id) for message in messages],
        source_refs=sorted(evidence_references(messages)[0]),
    )


def index_legacy_memos(
    messages: Sequence[AnyMessage],
    is_memo_request: Callable[[str], bool],
) -> list[MemoVersion]:
    """Adopt evidence-backed memos without changing an older checkpoint's messages.

    Earlier answers have no trustworthy generation timestamp. Mark it unknown
    rather than assigning the migration time to a historical document.
    """
    memos: list[MemoVersion] = []
    for group in turns(messages):
        if not is_memo_request(str(group[0].content)):
            continue
        memo = memo_from_turn(group, memos, "unknown (legacy checkpoint)")
        if memo is not None:
            memos.append(memo)
    return memos
