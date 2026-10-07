"""Rules shared by every adapter that calls an HTTP service."""

from __future__ import annotations

import ssl
from pathlib import Path

import httpx

from ai_agent_lib_core.contracts import ConfigurationError

__all__ = ["checked_base_url", "tls_verification"]

_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_ALLOW_HTTP_HINT = "set allow_http to use plain http inside a private network"


def checked_base_url(
    what: str, value: str, *, allow_http: bool, hint: str = _ALLOW_HTTP_HINT
) -> str:
    """Return ``value`` without a trailing slash if it is a safe base URL.

    Plain http is accepted for this machine, which is how a sidecar is reached,
    and elsewhere only when ``allow_http`` says so.

    Args:
        what: Names the thing the URL belongs to, in an error.
        value: The URL.
        allow_http: Whether plain http to another host is accepted.
        hint: What the error says a reader can do about a refused plain http URL.

    Raises:
        ConfigurationError: If the URL is malformed, carries credentials, a
            query or a fragment, or uses plain http where that is not allowed.
            The URL itself is not repeated, because it may hold credentials.
    """
    try:
        url = httpx.URL(value)
    except httpx.InvalidURL:
        raise ConfigurationError(f"{what}: the URL is not valid") from None
    if url.scheme not in {"http", "https"} or not url.host:
        raise ConfigurationError(f"{what}: the URL must be an http(s) URL")
    if url.userinfo or url.query or url.fragment:
        raise ConfigurationError(
            f"{what}: the URL must not hold credentials, a query or a fragment"
        )
    if url.scheme == "http" and url.host not in _LOCAL_HOSTS and not allow_http:
        raise ConfigurationError(f"{what}: the URL must use https; {hint}")
    return str(url).rstrip("/")


def tls_verification(what: str, ca_file: Path | None) -> ssl.SSLContext | bool:
    """Return what an HTTP client needs to verify the service's certificate.

    Raises:
        ConfigurationError: If ``ca_file`` cannot be read as a CA file.
    """
    if ca_file is None:
        return True
    try:
        return ssl.create_default_context(cafile=str(ca_file))
    except (OSError, ssl.SSLError):
        raise ConfigurationError(f"{what}: ca_file could not be read as a CA file") from None
