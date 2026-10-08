"""Removing anything shaped like a credential from text that may be shown.

Error messages, log lines and command output are read by people and kept by
log stores. Text that came from somewhere else, such as the message of a
third-party exception, passes through :func:`redact` before it is shown.
"""

from __future__ import annotations

import re

__all__ = ["REDACTED", "redact", "shorten"]

REDACTED = "[redacted]"

_CREDENTIALS = (
    # An Authorization header value, whatever follows the scheme.
    re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"),
    # A JSON web token.
    re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]*"),
    # An AWS access key ID.
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    # key=value and "key": "value" where the key names a credential.
    re.compile(
        r"(?i)([\"']?[A-Za-z0-9_-]*(?:password|passwd|secret|token|api[_-]?key|authorization)"
        r"[A-Za-z0-9_-]*[\"']?\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;&]+)"
    ),
    # Credentials inside a URL: scheme://user:password@host
    re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^\s/@:]+:[^\s/@]+@"),
)


def redact(text: str) -> str:
    """Return ``text`` with anything shaped like a credential replaced."""
    for index, pattern in enumerate(_CREDENTIALS):
        if index in (0, 3):
            text = pattern.sub(rf"\1{REDACTED}", text)
        elif index == 4:
            text = pattern.sub(rf"\1{REDACTED}@", text)
        else:
            text = pattern.sub(REDACTED, text)
    return text


def shorten(text: str, limit: int) -> str:
    """Return ``text`` cut to ``limit`` characters, marking the cut."""
    return text if len(text) <= limit else text[: max(limit - 1, 0)] + "…"
