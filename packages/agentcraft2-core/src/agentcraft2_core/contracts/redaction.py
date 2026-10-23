"""Removing anything shaped like a credential from text that may be shown.

Error messages, log lines and command output are read by people and kept by
log stores. Text that came from somewhere else, such as the message of a
third-party exception, passes through :func:`redact` before it is shown.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal
from itertools import islice
from typing import cast

__all__ = ["REDACTED", "query_parameters", "redact", "shorten"]

REDACTED = "[redacted]"

_CREDENTIALS = (
    # An Authorization header value, whatever follows the scheme.
    re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"),
    # A JSON web token.
    re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]*"),
    # An AWS access key ID.
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    # key=value and "key": "value" where the key names a credential.
    # Start at a key boundary so long non-credential words are not scanned again
    # from every character (quadratic work for large model/tool output).
    re.compile(
        r"(?i)(?<![A-Za-z0-9_-])"
        r"([\"']?[A-Za-z0-9_-]*(?:password|passwd|secret|token|api[_-]?key|authorization)"
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


_SENSITIVE = re.compile(
    r"password|passwd|secret|token|api.?key|authorization|cookie|credential|"
    r"private.?key|connection.?string|email|phone|ssn|iban|account.?number|card.?number",
    re.IGNORECASE,
)


def _scalar(value: object) -> object:
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str | date | datetime | Decimal):
        return shorten(redact(str(value)), 256)
    # Never invoke arbitrary repr/str methods, including secret wrappers.
    return REDACTED


def query_parameters(parameters: Mapping[str, object]) -> dict[str, object]:
    """Mask sensitive keys recursively and bound depth, items and scalar length.

    Search terms and client identifiers remain useful in local logs. This is
    credential masking, not anonymization; the log formatter omits parameters
    completely when local detail logging is off.
    """
    remaining = 32

    def safe(value: object, depth: int = 0) -> object:
        nonlocal remaining
        if depth > 3 or remaining <= 0:
            return "[truncated]"
        if isinstance(value, Mapping):
            result: dict[str, object] = {}
            for key, item in islice(value.items(), 32):
                if remaining <= 0:
                    result["..."] = "[truncated]"
                    break
                remaining -= 1
                label = str(key)
                result[shorten(redact(label), 80)] = (
                    REDACTED if _SENSITIVE.search(label) else safe(item, depth + 1)
                )
            if len(value) > 32:
                result["..."] = "[truncated]"
            return result
        if isinstance(value, list | tuple):
            result_list: list[object] = []
            for item in value[:32]:
                if remaining <= 0:
                    result_list.append("[truncated]")
                    break
                remaining -= 1
                result_list.append(safe(item, depth + 1))
            if len(value) > 32:
                result_list.append("[truncated]")
            return result_list
        return _scalar(value)

    return cast(dict[str, object], safe(parameters))
