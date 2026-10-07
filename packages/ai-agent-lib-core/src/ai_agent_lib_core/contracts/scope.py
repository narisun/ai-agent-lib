"""Tenant, subject and application scope, and the single storage-key builder.

Every store in the library is addressed through a :class:`Scope`. Because the
key is built in exactly one place, two different scopes can never collide on a
storage key, whatever characters their identifiers contain.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote, unquote

from ai_agent_lib_core.contracts._validation import require_identifier

__all__ = ["Scope"]

_SEPARATOR = "/"
_SCOPE_FIELDS = 3  # tenant, subject, application


def _encode(component: str) -> str:
    # ``safe=""`` percent-encodes the separator too, so an encoded component
    # never contains it and the joined key can be split back unambiguously.
    return quote(component, safe="")


@dataclass(frozen=True, slots=True)
class Scope:
    """The isolation boundary for stored data.

    Attributes:
        tenant: The tenant that owns the data.
        subject: The person or service the data belongs to.
        application: The agent or MCP server that wrote the data.
    """

    tenant: str
    subject: str
    application: str

    def __post_init__(self) -> None:
        require_identifier("tenant", self.tenant)
        require_identifier("subject", self.subject)
        require_identifier("application", self.application)

    def key(self, *parts: str) -> str:
        """Build the storage key for ``parts`` inside this scope.

        The mapping from ``(scope, parts)`` to a key is one-to-one: different
        inputs always give different keys, and :meth:`parse_key` reverses it.
        """
        return _SEPARATOR.join(_encode(component) for component in self.namespace(*parts))

    def namespace(self, *parts: str) -> tuple[str, ...]:
        """Return the scope and ``parts`` as a tuple, for stores keyed by tuple."""
        for index, part in enumerate(parts):
            require_identifier(f"parts[{index}]", part)
        return (self.tenant, self.subject, self.application, *parts)

    @classmethod
    def parse_key(cls, key: str) -> tuple[Scope, tuple[str, ...]]:
        """Split a key made by :meth:`key` back into its scope and parts.

        Raises:
            ValueError: If ``key`` was not produced by :meth:`key`.
        """
        components = [unquote(component) for component in key.split(_SEPARATOR)]
        if len(components) < _SCOPE_FIELDS:
            raise ValueError("not a scoped key: fewer than three components")
        tenant, subject, application, *parts = components
        scope = cls(tenant=tenant, subject=subject, application=application)
        if scope.key(*parts) != key:
            raise ValueError("not a scoped key: it does not round-trip")
        return scope, tuple(parts)
