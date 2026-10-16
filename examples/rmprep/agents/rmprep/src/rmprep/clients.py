"""Recover client names only from governed lookup results and saved memo evidence."""

import re
from collections.abc import Sequence
from dataclasses import dataclass

from langchain_core.messages import AIMessage, AnyMessage, ToolMessage

from rmprep.memos import MemoVersion, turns
from rmprep.response_review import tool_payload


@dataclass(frozen=True)
class ClientLookup:
    """The clients returned by one search and the exchange that established them."""

    clients: dict[str, str]
    message_ids: tuple[str, ...]
    complete: bool


def named_clients(question: str, known_names: dict[str, list[str]]) -> set[str]:
    """Resolve explicit UENs and known names, never a broad ambiguous search alias."""
    found = set(re.findall(r"\b[A-Z]{4}-\d{4}\b", question.upper()))
    text = question.casefold()
    found.update(
        uen
        for uen, aliases in known_names.items()
        if any(
            re.search(r"(?<!\w)" + re.escape(alias.casefold()) + r"(?!\w)", text)
            for alias in aliases
            if alias.strip()
        )
    )
    return found


def lookup_in_turn(messages: Sequence[AnyMessage]) -> ClientLookup | None:
    """Keep a search result set distinct from resolving one ambiguous name."""
    for message in reversed(messages):
        payload = tool_payload(message, include_lookup=True)
        if (
            payload.get("status") not in {"resolved", "ambiguous", "not_found"}
            or "matches" not in payload
        ):
            continue
        matches = payload["matches"]
        if not isinstance(matches, list):
            continue
        clients = {
            row["uen"]: row["name"]
            for row in matches
            if isinstance(row, dict)
            and isinstance(row.get("uen"), str)
            and re.fullmatch(r"[A-Z]{4}-\d{4}", row["uen"])
            and isinstance(row.get("name"), str)
        }
        complete = payload.get("truncated") is False and len(clients) == len(matches)
        return ClientLookup(clients, tuple(m.id for m in messages if m.id), complete)
    return None


def recent_client_lookup(messages: Sequence[AnyMessage]) -> ClientLookup | None:
    """Find the latest search in this topic, stopping at an explicit client/memo switch.

    Messages must exclude the current question. Older checkpoints need no migration:
    search results are recovered from their original governed tool messages.
    """
    names = client_names(messages, [])
    for group in reversed(turns(messages)):
        lookup = lookup_in_turn(group)
        if lookup is not None:
            return lookup
        if named_clients(str(group[0].content), names) or re.search(
            r"\bmemo\b|\bmemo-[a-z0-9]{12}\b", str(group[0].content), re.IGNORECASE
        ):
            return None
    return None


def client_names(
    messages: Sequence[AnyMessage], memos: Sequence[MemoVersion]
) -> dict[str, list[str]]:
    """Remember successful lookup terms, without guessing aliases from assistant prose.

    For example, a successful search for Canada Goose establishes that phrase as
    a name for the returned UEN even when the official name includes Holdings Inc.
    Ambiguous search terms and truncated lookups establish no alias. Complete
    result sets retain their canonical names. Shared names remain shared so
    selection can ask the user to disambiguate a single-client request.
    """
    names: dict[str, list[str]] = {}
    for memo in memos:
        names.setdefault(memo["uen"], []).append(memo["client_name"])
    calls = {
        call["id"]: call
        for message in messages
        if isinstance(message, AIMessage)
        for call in message.tool_calls
    }
    for message in messages:
        payload = tool_payload(message, include_lookup=True)
        # A complete result set establishes canonical names for all listed clients,
        # but never makes its broad search term an alias for any one of them.
        if payload.get("status") == "ambiguous" and payload.get("truncated") is False:
            lookup = lookup_in_turn([message])
            if lookup is not None and lookup.complete and len(lookup.clients) > 1:
                for matched_uen, name in lookup.clients.items():
                    names.setdefault(matched_uen, []).append(name)
        if (
            not isinstance(message, ToolMessage)
            or payload.get("status") != "resolved"
            or payload.get("truncated") is not False
        ):
            continue
        uen = payload.get("resolved_uen")
        matches = payload.get("matches", [])
        if not isinstance(uen, str) or not isinstance(matches, list):
            continue
        matched = [row for row in matches if isinstance(row, dict) and row.get("uen") == uen]
        if len(matched) != 1 or not isinstance(matched[0].get("name"), str):
            continue
        aliases = names.setdefault(uen, [])
        aliases.append(matched[0]["name"])
        call = calls.get(message.tool_call_id)
        if call and call["name"] == message.name:
            search = call["args"].get("search")
            if isinstance(search, str) and search.strip():
                aliases.append(search.strip())
    return {uen: sorted(set(aliases)) for uen, aliases in names.items()}


def resolved_clients(messages: Sequence[AnyMessage]) -> set[str]:
    """Read resolved lookups or successful evidence identities from this turn."""
    clients: set[str] = set()
    for message in messages:
        payload = tool_payload(message, include_lookup=True)
        if payload.get("status") == "not_found":
            continue
        uen = payload.get("uen")
        if isinstance(uen, str) and uen:
            clients.add(uen)
        if payload.get("status") == "resolved" and payload.get("truncated") is False:
            resolved = payload.get("resolved_uen")
            if isinstance(resolved, str) and resolved:
                clients.add(resolved)
    return clients
