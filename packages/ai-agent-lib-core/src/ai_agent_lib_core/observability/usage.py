"""Normalize provider token counts without storing arbitrary response metadata."""

from collections.abc import Mapping
from typing import Literal

from ai_agent_lib_core.contracts.diagnostics import TokenUsage, UsageField


def _count(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _cache_write(fields: Mapping[str, int]) -> int | None:
    """Handle LangChain's Anthropic TTL breakdown without counting it twice."""
    total = fields.get("input_token_details.cache_creation")
    short = fields.get("input_token_details.ephemeral_5m_input_tokens")
    long = fields.get("input_token_details.ephemeral_1h_input_tokens")
    if short is not None and long is not None:
        return total if total else short + long
    if short or long:
        return total if total else None
    return total


def normalize_usage(
    usage: Mapping[str, object] | None,
    *,
    source: Literal["langchain", "anthropic"],
    kind: Literal["reported", "simulated", "estimated"] = "reported",
) -> TokenUsage:
    """Translate a known usage shape, preserving missing counts as unknown.

    Prefer LangChain usage_metadata when present. Its input_tokens already includes
    caching. Raw Anthropic input_tokens excludes both cache categories. Never merge
    both representations of the same response or copy arbitrary metadata fields.
    """
    if usage is None:
        return TokenUsage()
    fields: dict[str, int] = {}
    names: tuple[str, ...] = ("input_tokens", "output_tokens", "total_tokens")
    if source == "anthropic":
        names += ("cache_read_input_tokens", "cache_creation_input_tokens")
    for name in names:
        value = _count(usage.get(name))
        if value is not None:
            fields[name] = value
    details = usage.get("input_token_details")
    if source == "langchain" and isinstance(details, Mapping):
        for name in (
            "cache_read",
            "cache_creation",
            "ephemeral_5m_input_tokens",
            "ephemeral_1h_input_tokens",
        ):
            value = _count(details.get(name))
            if value is not None:
                fields[f"input_token_details.{name}"] = value
    read_key, write_key = (
        ("cache_read_input_tokens", "cache_creation_input_tokens")
        if source == "anthropic"
        else ("input_token_details.cache_read", "input_token_details.cache_creation")
    )
    read, write = fields.get(read_key), fields.get(write_key)
    if source == "langchain":
        write = _cache_write(fields)
    incoming = fields.get("input_tokens")
    if source == "anthropic":
        incoming = (
            incoming + read + write
            if incoming is not None and read is not None and write is not None
            else None
        )
    # A malformed provider count is unknown, rather than a reason to lose chat.
    if incoming is not None and (read or 0) + (write or 0) > incoming:
        read = write = None
    return TokenUsage(
        source=source,
        kind=kind,
        input_tokens=incoming,
        output_tokens=fields.get("output_tokens"),
        cache_read_tokens=read,
        cache_write_tokens=write,
        source_fields=tuple(
            UsageField(name=key, value=value) for key, value in sorted(fields.items())
        ),
    )
