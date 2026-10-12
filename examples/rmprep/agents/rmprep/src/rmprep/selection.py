"""Resolve explicit memo references before a specialist interprets business evidence."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Literal, TypedDict

from rmprep.memos import MemoVersion

_MONTHS = (
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
)


class ContextSelection(TypedDict):
    """A per-turn decision; dates describe the selected memo, not retrieval time."""

    memo_ids: list[str]
    uen: str
    meeting_date: str
    retrieval_date: str
    mode: Literal["snapshot", "refresh", "new"]
    reason: str
    clarification: str


def _choice(
    memos: Sequence[MemoVersion] = (),
    *,
    uen: str = "",
    meeting_date: str = "",
    mode: Literal["snapshot", "refresh", "new"] = "snapshot",
    reason: str = "active memo",
    clarification: str = "",
) -> ContextSelection:
    clients = {memo["uen"] for memo in memos}
    dates = {memo["meeting_date"] for memo in memos}
    return ContextSelection(
        memo_ids=[memo["memo_id"] for memo in memos],
        uen=uen or (next(iter(clients)) if len(clients) == 1 else ""),
        meeting_date=meeting_date or (next(iter(dates)) if len(dates) == 1 else ""),
        retrieval_date="",
        mode=mode,
        reason=reason,
        clarification=clarification,
    )


def _clarify(candidates: Sequence[MemoVersion], reason: str) -> ContextSelection:
    options = "; ".join(
        f"{memo['memo_id']} ({memo['uen']}, version {memo['version']}, "
        f"as of {memo['meeting_date']})"
        for memo in candidates[-6:]
    )
    return _choice(
        reason=reason,
        clarification=f"{reason}. Please specify a memo reference, client and date."
        + (
            f" Available memos: {options}."
            if options
            else " No matching memo is saved in this thread."
        ),
    )


def _dated_candidates(question: str, candidates: list[MemoVersion]) -> list[MemoVersion]:
    dates = re.findall(r"\b\d{4}-\d{2}-\d{2}\b", question)
    if dates:
        return [memo for memo in candidates if memo["meeting_date"] in dates]
    selected = [
        str(i).zfill(2)
        for i, month in enumerate(_MONTHS, 1)
        if re.search(rf"\b{month}\b", question)
    ]
    return (
        [memo for memo in candidates if memo["meeting_date"][5:7] in selected]
        if selected
        else candidates
    )


def _historical_candidates(
    question: str, candidates: list[MemoVersion], active_id: str
) -> list[MemoVersion]:
    versions = {
        number
        for pair in re.findall(
            r"\b(?:versions?\s+|v)(\d+)\b(?:\s+(?:and|with|vs\.?)\s+(?:version\s+|v)?(\d+)\b)?",
            question,
        )
        for number in pair
        if number
    }
    if versions:
        return [memo for memo in candidates if str(memo["version"]) in versions]
    if "latest" in question and "previous" in question and "compare" in question:
        return candidates[-2:]
    if "first" in question and "latest" in question and "compare" in question:
        return [candidates[0], candidates[-1]] if len(candidates) > 1 else candidates
    if re.search(r"\b(first|original)\s+memo\b", question):
        return candidates[:1]
    if re.search(r"\b(latest|newest|most recent)\s+memo\b", question):
        return candidates[-1:]
    if re.search(r"\b(previous|earlier|older)\s+memo\b", question):
        position = next(
            (i for i, memo in enumerate(candidates) if memo["memo_id"] == active_id),
            len(candidates) - 1,
        )
        older = candidates[: max(0, position)]
        return older[-1:] if "previous" in question else older
    return _dated_candidates(question, candidates)


def _names_new_client(question: str, known_clients: set[str]) -> bool:
    """Treat an unrecognized named client as a new lookup, not the active memo."""
    if known_clients:
        return False
    subject = re.search(
        r"\b(?:for|about)\s+([A-Za-z][A-Za-z &'\u2019-]{2,})", question, re.IGNORECASE
    )
    if subject is None:
        subject = re.search(r"^memo:\s*([A-Za-z][A-Za-z &'\u2019-]{2,})", question, re.IGNORECASE)
    if subject is None:
        return False
    first = subject[1].casefold().split()[0]
    # These are workflow words and references to the established conversation,
    # rather than a newly named company. Exact known client names are handled above.
    context_words = {
        "this",
        "that",
        "the",
        "our",
        "my",
        "a",
        "an",
        "same",
        "next",
        "previous",
        "today",
        "tomorrow",
        "yesterday",
        "me",
        "prepare",
        "create",
        "generate",
        "refresh",
        "update",
        "write",
        "please",
        "credit",
        "covenants",
        "covenant",
        "working",
        "financial",
        "payments",
        "payment",
        "liquidity",
        "treasury",
        "version",
        "versions",
        "meeting",
    }
    return first not in context_words and first not in _MONTHS


def _has_selector(question: str) -> bool:
    return bool(
        re.search(
            r"\b(previous|earlier|older|first|original|latest|newest|recent|versions?)\b|\d{4}-|\bv\d+\b"
            + "|"
            + "|".join(rf"\b{month}\b" for month in _MONTHS),
            question,
        )
    )


def _new_memo_selection(
    question: str,
    candidates: list[MemoVersion],
    active_id: str,
    clients: set[str],
    uen: str,
) -> ContextSelection:
    dates = re.findall(r"\b\d{4}-\d{2}-\d{2}\b", question)
    if len(clients) > 1 or len(dates) > 1:
        return _clarify(candidates, "Create one client memo for one meeting date at a time")
    active = next((m for m in candidates if m["memo_id"] == active_id), None)
    if _names_new_client(question, clients):
        uen, active = "", None
    return _choice(
        uen=uen,
        meeting_date=dates[0] if dates else (active["meeting_date"] if active else ""),
        mode="new",
        reason="new memo; retrieve a fresh evidence pack",
    )


def _reference_selection(
    question: str,
    candidates: list[MemoVersion],
    active_id: str,
    uen: str,
    *,
    has_memos: bool,
    comparison: bool,
    mode: Literal["snapshot", "refresh", "new"],
) -> ContextSelection:
    if re.search(
        r"\byesterday\b|\blast (week|month|year|monday|tuesday|wednesday|thursday|friday)\b",
        question,
    ):
        return _clarify(
            candidates, "Use an explicit date or memo reference for a historical document"
        )
    if not has_memos and not _has_selector(question):
        return _choice(uen=uen, mode=mode, reason="legacy conversation without versioned memos")
    selected = _historical_candidates(question, candidates, active_id)
    if not comparison and not _has_selector(question):
        selected = [m for m in candidates if m["memo_id"] == active_id] or candidates[-1:]
    if len(selected) != (2 if comparison else 1):
        return _clarify(
            selected or candidates, "The requested memo selection is ambiguous or unavailable"
        )
    return _choice(selected, uen=uen, mode=mode, reason="explicit reference")


def _selection_mode(
    question: str, creating: bool, refreshing: bool
) -> Literal["snapshot", "refresh", "new"]:
    if creating:
        return "new"
    if refreshing and not re.search(r"\b(latest|newest|most recent)\s+memo\b", question):
        return "refresh"
    return "snapshot"


def select_context(
    question: str,
    memos: Sequence[MemoVersion],
    active_id: str,
    active_client: str,
    *,
    creating_memo: bool,
) -> ContextSelection:
    """Resolve known references deterministically and ask when a reference is ambiguous.

    Exact memo IDs work across clients within this authorized thread. Dates and
    version numbers are scoped to an explicit client or the active client.
    """
    text = question.casefold()
    explicit_ids = re.findall(r"\bmemo-[a-z0-9]{12}\b", text)
    clients = set(re.findall(r"\b[A-Z]{4}-\d{4}\b", question.upper()))
    clients.update(
        memo["uen"]
        for memo in memos
        if memo["client_name"] != memo["uen"] and memo["client_name"].casefold() in text
    )
    uen = next(iter(clients)) if len(clients) == 1 else (active_client if not clients else "")
    candidates = [memo for memo in memos if not uen or memo["uen"] == uen]
    reference = bool(re.search(r"\bmemos?\b|\bversions?\s+\d|\bv\d+\b", text))
    comparison = bool(re.search(r"\bcompare\b|\bdifference(?:s)?\b|\bbetween\b", text))
    refreshing = bool(
        re.search(r"\b(latest|current|today|refresh|updated)\b|what.*changed|since then", text)
    )
    mode = _selection_mode(text, creating_memo, refreshing)
    if explicit_ids:
        uen = next(iter(clients)) if len(clients) == 1 else ""
        selected = [memo for memo in memos if memo["memo_id"] in explicit_ids]
        if len(selected) != len(set(explicit_ids)) or (
            clients and any(m["uen"] not in clients for m in selected)
        ):
            return _clarify(
                candidates, "The memo reference does not match a saved memo for that client"
            )
    elif creating_memo:
        return _new_memo_selection(question, candidates, active_id, clients, uen)
    elif reference:
        if _names_new_client(question, clients):
            return _clarify([], "No saved memo is identified for that named client")
        return _reference_selection(
            text,
            candidates,
            active_id,
            uen,
            has_memos=bool(memos),
            comparison=comparison,
            mode=mode,
        )
    else:
        if _names_new_client(question, clients):
            return _choice(mode="new", reason="resolve the newly named client with find_clients")
        dates = re.findall(r"\b\d{4}-\d{2}-\d{2}\b", question)
        if dates and not refreshing:
            return _choice(
                uen=uen, meeting_date=dates[-1], mode="new", reason="explicit evidence date"
            )
        selected = [m for m in candidates if m["memo_id"] == active_id]
        if clients and not selected:
            selected = candidates[-1:]
    if reference or explicit_ids:
        required = 2 if comparison else 1
        if len(selected) != required:
            return _clarify(
                selected or candidates, "The requested memo selection is ambiguous or unavailable"
            )
    if clients and len(clients) > 1 and not comparison:
        return _clarify(candidates, "Please choose one client")
    return _choice(
        selected,
        uen=uen,
        mode=mode,
        reason="explicit reference" if reference or explicit_ids else "active client",
    )
