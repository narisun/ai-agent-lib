"""Resolve client groups and memo references before a specialist reads business evidence."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Literal, NotRequired, TypedDict

from rmprep.clients import ClientLookup, named_clients
from rmprep.focus import ConversationFocus
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
    client_uens: NotRequired[list[str]]
    lookup_message_ids: NotRequired[list[str]]
    evidence_message_ids: NotRequired[list[str]]


def _choice(
    memos: Sequence[MemoVersion] = (),
    *,
    uen: str = "",
    meeting_date: str = "",
    mode: Literal["snapshot", "refresh", "new"] = "snapshot",
    reason: str = "active memo",
    clarification: str = "",
    client_uens: Sequence[str] = (),
    lookup_message_ids: Sequence[str] = (),
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
        client_uens=sorted(set(client_uens) | clients | ({uen} if uen else set())),
        lookup_message_ids=list(lookup_message_ids),
    )


def _client_group_selection(
    question: str,
    clients: set[str],
    recent: ClientLookup | None,
    active: Sequence[str],
    mode: Literal["snapshot", "refresh", "new"],
    explicit_group: bool,
) -> ContextSelection | None:
    """Resolve a listed group or an established comparison without choosing one member."""
    has_group = len(recent.clients) > 1 if recent is not None else len(active) > 1
    plural = bool(
        re.search(
            r"\b(?:(?:these|those|the)\s+(?:two\s+)?|two\s+)(?:clients|companies)\b",
            question,
        )
    ) or (has_group and bool(re.search(r"\b(?:both|them|their)\b", question)))
    if len(clients) == 1 or _names_new_client(question, clients):
        return None
    if len(clients) > 1:
        if not explicit_group:
            return None
        chosen = sorted(clients)
    elif plural:
        chosen = list(recent.clients) if recent is not None else list(active)
        pair = bool(re.search(r"\b(?:both|two|pair)\b", question))
        if (
            (recent is not None and not recent.complete)
            or len(chosen) < 2
            or (pair and len(chosen) != 2)
        ):
            return _choice(
                reason="client group is ambiguous or unavailable",
                clarification="Please specify the client names or UENs to compare; the current search does not identify that group completely.",
            )
    elif len(active) > 1:
        chosen = list(active)
    else:
        return None
    dates = re.findall(r"\b\d{4}-\d{2}-\d{2}\b", question)
    anchors = (
        recent.message_ids if recent is not None and set(chosen) <= set(recent.clients) else ()
    )
    return _choice(
        client_uens=chosen,
        lookup_message_ids=anchors,
        meeting_date=dates[-1] if dates else "",
        mode=mode,
        reason="selected client group",
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


def _memo_dates(question: str) -> list[str]:
    """Read dates attached to a document reference, not events discussed inside it."""
    date = r"(?:\d{4}-\d{2}-\d{2}|(?:" + "|".join(_MONTHS) + r")(?:\s+\d{4})?)"
    dates = date + r"(?:\s*(?:,|and|with|vs\.?)\s*" + date + r")*"
    phrases = re.finditer(
        rf"\b(?:(?P<before>{dates})\s+memos?\b|memos?\s+"
        rf"(?:(?:from|dated|as of|for|on|in)\s+)?(?P<after>{dates})\b)",
        question,
    )
    return [
        match[0]
        for phrase in phrases
        for match in re.finditer(date, phrase["before"] or phrase["after"])
    ]


def _dated_candidates(question: str, candidates: list[MemoVersion]) -> list[MemoVersion]:
    selectors = _memo_dates(question)
    if not selectors:
        return candidates
    selected = []
    for memo in candidates:
        for selector in selectors:
            parts = selector.split()
            matches = memo["meeting_date"] == selector
            if parts[0] in _MONTHS:
                matches = int(memo["meeting_date"][5:7]) == _MONTHS.index(parts[0]) + 1
                matches &= len(parts) == 1 or memo["meeting_date"][:4] == parts[1]
            if matches:
                selected.append(memo)
                break
    return selected


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
    """Recognize explicit company naming, never arbitrary words after 'about'.

    Unknown names can be resolved by find_clients. Only successful resolution
    changes durable focus; this predicate merely isolates a proposed lookup.
    """
    if known_clients:
        return False
    subject = re.search(r"\b(?:for|about)\s+(.+?)(?:[?.!]|$)", question, re.IGNORECASE)
    if subject is None:
        subject = re.search(r"^memo:\s*(.+)", question, re.IGNORECASE)
    if subject is None:
        return False
    name = subject[1].strip()
    explicit = re.search(
        r"^(?:another|a different|client|company)\b|\b(?:company|inc|ltd|llc|corporation|holdings)\b",
        name,
        re.IGNORECASE,
    )
    # Capitalization is not evidence of a company: "Cash Flow" and "Total Debt"
    # are ordinary topics. An explicitly quoted name can request a fresh lookup.
    quoted_name = re.match(r"""["']\S.+["']""", name)
    return bool(explicit or quoted_name)


def _has_selector(question: str) -> bool:
    return bool(
        re.search(
            r"\b(?:previous|earlier|older|first|original|latest|newest|most recent)\s+memos?\b"
            r"|\bversions?\s+\d|\bv\d+\b|\bcompare\b.*\b(?:latest|previous|first)\b",
            question,
        )
    ) or bool(_memo_dates(question))


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
        r"\b(?:yesterday|last (?:week|month|year|monday|tuesday|wednesday|thursday|friday))['\u2019]?(?:s)?\s+memo\b"
        r"|\bmemo\s+(?:from\s+)?(?:yesterday|last (?:week|month|year|monday|tuesday|wednesday|thursday|friday))\b",
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


def _select_context(
    question: str,
    memos: Sequence[MemoVersion],
    active_id: str,
    active_client: str,
    *,
    creating_memo: bool,
    known_names: Mapping[str, Sequence[str]] | None = None,
    recent_lookup: ClientLookup | None = None,
    active_clients: Sequence[str] = (),
) -> ContextSelection:
    """Resolve known references deterministically and ask when a reference is ambiguous.

    Exact memo IDs work across clients within this authorized thread. Dates and
    version numbers are scoped to an explicit client or the active client.
    """
    text = question.casefold()
    explicit_ids = re.findall(r"\bmemo-[a-z0-9]{12}\b", text)
    names = {uen: list(aliases) for uen, aliases in (known_names or {}).items()}
    for memo in memos:
        names.setdefault(memo["uen"], []).append(memo["client_name"])
    clients = named_clients(question, names)
    unique_names = {
        uen: [
            alias
            for alias in aliases
            if sum(
                alias.casefold() in {other.casefold() for other in others}
                for others in names.values()
            )
            == 1
        ]
        for uen, aliases in names.items()
    }
    explicit_group = len(named_clients(question, unique_names)) > 1
    uen = next(iter(clients)) if len(clients) == 1 else (active_client if not clients else "")
    candidates = [memo for memo in memos if not uen or memo["uen"] == uen]
    reference = bool(re.search(r"\bmemos?\b|\bversions?\s+\d|\bv\d+\b", text))
    comparison = bool(re.search(r"\bcompare\b|\bdifference(?:s)?\b|\bbetween\b", text))
    refreshing = bool(
        re.search(r"\b(latest|current|today|refresh|updated)\b|what.*changed|since then", text)
    )
    mode = _selection_mode(text, creating_memo, refreshing)
    if not (creating_memo or reference or explicit_ids):
        group = _client_group_selection(
            text, clients, recent_lookup, active_clients, mode, explicit_group
        )
        if group is not None:
            return group
    if len(clients) > 1 and not explicit_group and not explicit_ids:
        return _clarify(candidates, "Please choose one client")
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
    if creating_memo and explicit_ids:
        dates = re.findall(r"\b\d{4}-\d{2}-\d{2}\b", question)
        if len(selected) != 1 or len(dates) > 1:
            return _clarify(selected, "Create one client memo for one meeting date at a time")
        return _choice(
            uen=selected[0]["uen"],
            meeting_date=dates[0] if dates else selected[0]["meeting_date"],
            mode="new",
            reason="new version of the referenced memo",
        )
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


def select_context(
    question: str,
    memos: Sequence[MemoVersion],
    active_id: str,
    active_client: str,
    *,
    creating_memo: bool,
    known_names: Mapping[str, Sequence[str]] | None = None,
    recent_lookup: ClientLookup | None = None,
    active_clients: Sequence[str] = (),
    focus: ConversationFocus | None = None,
) -> ContextSelection:
    """Resolve explicit requests, then carry accepted focus into ordinary follow-ups."""
    selection = _select_context(
        question,
        memos,
        active_id,
        active_client,
        creating_memo=creating_memo,
        known_names=known_names,
        recent_lookup=recent_lookup,
        active_clients=active_clients,
    )
    dates = re.findall(r"\b\d{4}-\d{2}-\d{2}\b", question)
    if dates and selection["mode"] == "refresh" and not selection["clarification"]:
        selection = {**selection, "retrieval_date": dates[-1]}
    if focus is None or creating_memo or selection["clarification"]:
        return selection
    same_clients = set(selection.get("client_uens", [])) == set(focus["client_uens"])
    explicit = selection["reason"] == "explicit reference" or bool(
        re.search(r"\bmemo-[a-z0-9]{12}\b|\b\d{4}-\d{2}-\d{2}\b", question)
    )
    if same_clients and not explicit and selection["mode"] == "snapshot":
        selection = {
            **selection,
            "memo_ids": list(focus["memo_ids"]),
            "meeting_date": focus["evidence_date"],
            "retrieval_date": focus["evidence_date"],
            "evidence_message_ids": list(focus["message_ids"]),
            "reason": "accepted conversation focus",
        }
    return selection
