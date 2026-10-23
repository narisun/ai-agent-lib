"""Render JSON settings for startup diagnostics without exposing sensitive fields."""

from __future__ import annotations

import json
import math
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

MASKED = "********"
_MAX_DEPTH = 16
_SENSITIVE_PARTS = (
    "password",
    "passwd",
    "secret",
    "credential",
    "authorization",
    "authentication",
    "apikey",
    "accesskey",
    "privatekey",
    "publickey",
    "signingkey",
    "encryptionkey",
    "cookie",
    "signature",
    "bearer",
)
_SENSITIVE_NAMES = frozenset(
    {
        "key",
        "pwd",
        "auth",
        "jwt",
        "sig",
        "dsn",
        "connectionstring",
        "subject",
        "username",
        "userid",
        "email",
        "phone",
        "phonenumber",
    }
)
# Token budgets and lifetimes are operational settings, not credential tokens.
_TOKEN_COUNTS = frozenset(
    {
        "tokens",
        "maxtokens",
        "maxinputtokens",
        "maxoutputtokens",
        "inputtokens",
        "outputtokens",
        "tokenbudget",
        "tokenlimit",
        "tokenttlseconds",
    }
)
# Custom headers can carry credentials under arbitrary names. Reveal values
# only for these ordinary content-negotiation headers; keep every header name.
_PUBLIC_HEADERS = frozenset(
    {"accept", "acceptencoding", "acceptlanguage", "contenttype", "useragent"}
)
_API_KEY = re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_-]{16,}\b")
_AUTH_QUERY_FIELDS = frozenset(
    {"code", "state", "session", "sessionid", "assertion", "samlresponse", "ticket"}
)


def _normalized(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.casefold())


def _sensitive(name: str, value: object) -> bool:
    normalized = _normalized(name)
    numeric = (
        value is None
        or isinstance(value, (int, float))
        or (isinstance(value, str) and value.isascii() and value.isdecimal())
    )
    if normalized in _TOKEN_COUNTS and numeric:
        return False
    return (
        normalized in _SENSITIVE_NAMES
        or normalized.endswith(("token", "tokens", "tokenhash", "tokenvalue"))
        or any(part in normalized for part in _SENSITIVE_PARTS)
    )


def _url(value: str) -> str:
    """Keep the endpoint and ordinary query options, masking credentials in the URL."""
    try:
        parts = urlsplit(value)
        _ = parts.port
        local_file = parts.scheme in {"file", "sqlite", "sqlite+aiosqlite"} and bool(parts.path)
        if (not parts.hostname and not local_file) or any(ord(char) < 32 for char in value):
            return MASKED
        authority = parts.netloc
        if parts.username is not None:
            authority = MASKED + "@" + authority.rsplit("@", 1)[-1]
        query = urlencode(
            [
                (
                    name,
                    MASKED
                    if _sensitive(name, item) or _normalized(name) in _AUTH_QUERY_FIELDS
                    else _text(item),
                )
                for name, item in parse_qsl(parts.query, keep_blank_values=True)
            ],
            safe="*",
        )
        path = _text(parts.path)
        fragment = MASKED if parts.fragment else ""
        if local_file and not authority:
            # urllib does not register sqlite as an authority-based scheme and
            # would collapse sqlite:/// into sqlite:/, changing its meaning.
            return parts.scheme + "://" + urlunsplit(("", "", path, query, fragment))
        return urlunsplit((parts.scheme, authority, path, query, fragment))
    except (ValueError, RecursionError):
        return MASKED


def _text(value: str) -> str:
    """Catch recognizable credentials even inside an otherwise public text field."""
    from agentcraft2_core.contracts.redaction import redact

    if re.search(r"-----BEGIN [A-Z ]*PRIVATE KEY-----", value):
        return MASKED
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", value):
        return _API_KEY.sub(MASKED, _url(value))
    return _API_KEY.sub(MASKED, redact(value))


def _sanitize(value: object, *, depth: int = 0, headers: bool = False) -> object:
    """Copy nested values, preserving JSON types and replacing sensitive subtrees."""
    if depth >= _MAX_DEPTH:
        return MASKED
    if isinstance(value, dict):
        result: dict[str, object] = {}
        for name, item in value.items():
            normalized = _normalized(name)
            if (headers and normalized not in _PUBLIC_HEADERS) or _sensitive(name, item):
                result[name] = MASKED
            elif "header" in normalized:
                result[name] = (
                    _sanitize(item, depth=depth + 1, headers=True)
                    if isinstance(item, dict)
                    else MASKED
                )
            else:
                result[name] = _sanitize(item, depth=depth + 1)
        return result
    if isinstance(value, list):
        return [_sanitize(item, depth=depth + 1, headers=headers) for item in value]
    if isinstance(value, str):
        clean = _text(value)
        return clean if len(clean) <= 180 else clean[:177] + "..."
    if isinstance(value, float) and not math.isfinite(value):
        return MASKED
    return value


def display_json(value: str) -> str:
    """Return one escaped JSON line; malformed or excessively nested input stays masked."""
    try:
        sanitized = _sanitize(json.loads(value))
        return json.dumps(sanitized, ensure_ascii=True, allow_nan=False)
    except (ValueError, RecursionError):
        # Never fall back to printing an unparsed configuration string.
        return MASKED
