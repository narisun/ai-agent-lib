"""What an agent and an MCP server built with this library agree on, on the wire."""

from __future__ import annotations

from collections.abc import Mapping

__all__ = [
    "META_AUTHORIZATION",
    "META_CLASSIFICATION_CEILING",
    "META_REQUEST_ID",
    "META_THREAD_ID",
    "is_error_result",
    "mcp_result_text",
]

META_REQUEST_ID = "agentlib/request_id"
"""Request metadata key: the caller's request ID, so both audit logs can be joined."""

META_THREAD_ID = "agentlib/thread_id"
"""Request metadata key: the caller's conversation or workflow ID."""

META_CLASSIFICATION_CEILING = "agentlib/classification_ceiling"
"""Request metadata key: the most sensitive data the caller's request may touch."""

META_AUTHORIZATION = "agentlib/authorization"
"""Request metadata key that carries the token when the transport has no headers.

Over HTTP the token travels in the ``Authorization`` header and this key is
ignored. It exists for in-process connections, which are how servers are
tested.
"""


def _field(item: object, name: str) -> object:
    if isinstance(item, Mapping):
        return item.get(name)
    return getattr(item, name, None)


def is_error_result(result: object) -> bool:
    """Return whether a tool result, as a model or as its wire form, reports an error."""
    return _field(result, "isError") is True or _field(result, "is_error") is True


def mcp_result_text(result: object) -> str:
    """Return the text of a tool result, as a model or as its wire form.

    Content that is not text is named, not included.
    """
    parts: list[str] = []
    content = _field(result, "content")
    for item in content if isinstance(content, list | tuple) else ():
        kind = _field(item, "type")
        text = _field(item, "text")
        if kind == "text" and isinstance(text, str):
            parts.append(text)
        else:
            parts.append(f"[{kind} content omitted]")
    return "\n".join(parts)
