"""Small validation helpers shared by the value types."""

from __future__ import annotations

__all__ = ["require_identifier"]


def require_identifier(field: str, value: object) -> str:
    """Return ``value`` if it is a usable identifier, otherwise raise.

    An identifier is a non-empty string with no surrounding whitespace and no
    control characters. The rule is deliberately loose about which printable
    characters are allowed, because tenants and subjects come from an identity
    provider the library does not control.

    Args:
        field: The field name, used in the error message.
        value: The candidate value.

    Raises:
        TypeError: If ``value`` is not a string.
        ValueError: If ``value`` is empty, padded or contains control characters.
    """
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string, not {type(value).__name__}")
    if not value:
        raise ValueError(f"{field} must not be empty")
    if value != value.strip():
        raise ValueError(f"{field} must not start or end with whitespace")
    if any(not ch.isprintable() for ch in value):
        raise ValueError(f"{field} must not contain control characters")
    return value
