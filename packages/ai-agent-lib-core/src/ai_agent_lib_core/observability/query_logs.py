"""Bounded local query parameters; operational logs never include result rows."""

import re
from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal
from itertools import islice
from typing import cast

from ai_agent_lib_core.contracts.redaction import REDACTED, redact, shorten

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
