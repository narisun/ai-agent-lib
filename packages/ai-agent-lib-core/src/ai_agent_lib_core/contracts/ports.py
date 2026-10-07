"""The ports: interfaces every adapter implements and every component depends on.

A component names the ports it needs in its constructor and never imports an
adapter. Ports are structural (:class:`typing.Protocol`), so an adapter does
not have to inherit from anything.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from pydantic import SecretStr

from ai_agent_lib_core.contracts.audit import AuditRecord, AuditValue
from ai_agent_lib_core.contracts.errors import AgentLibError
from ai_agent_lib_core.contracts.identity import Principal, RequestContext

__all__ = [
    "AuditSink",
    "ChatModelProvider",
    "CheckpointBackend",
    "Clock",
    "ConfigSource",
    "IdGenerator",
    "IdentityVerifier",
    "ModelCapabilities",
    "SecretsProvider",
    "SupportsAsyncClose",
    "SupportsValidation",
    "Telemetry",
    "TokenAuthenticator",
    "TokenExchanger",
]


class Clock(Protocol):
    """The source of time. Injected so tests can freeze it."""

    def now(self) -> datetime:
        """Return the current time as an aware UTC datetime."""
        ...

    def monotonic(self) -> float:
        """Return seconds from a clock that never goes backwards."""
        ...


class IdGenerator(Protocol):
    """The source of unique identifiers. Injected so tests are repeatable."""

    def new_id(self) -> str:
        """Return a new unique identifier."""
        ...


class ConfigSource(Protocol):
    """A read-only source of raw configuration values."""

    def get(self, name: str) -> str | None:
        """Return the raw value for ``name``, or ``None`` when it is not set."""
        ...

    def names(self) -> Iterable[str]:
        """Return every name the source holds."""
        ...


class AuditSink(Protocol):
    """Durable, fail-closed evidence.

    ``write`` returns only once the record is durably stored. If it cannot be
    stored the sink raises :class:`~ai_agent_lib_core.contracts.errors.IntegrityError`
    and the request that produced the record fails.
    """

    async def write(self, record: AuditRecord) -> None:
        """Store ``record`` durably or raise ``IntegrityError``."""
        ...


class Telemetry(Protocol):
    """Metadata-only operational signals. Never carries content."""

    def event(self, name: str, attributes: Mapping[str, AuditValue]) -> None:
        """Record that something happened."""
        ...

    def duration(self, name: str, seconds: float, attributes: Mapping[str, AuditValue]) -> None:
        """Record how long something took."""
        ...


class SecretsProvider(Protocol):
    """Resolves named credentials."""

    async def get_secret(self, name: str) -> SecretStr:
        """Return the secret called ``name``.

        Raises:
            ConfigurationError: If no such secret exists.
        """
        ...


class IdentityVerifier(Protocol):
    """Turns a presented credential into a verified principal."""

    async def verify(self, credential: str | None) -> Principal:
        """Return the principal for ``credential``.

        Raises:
            PolicyDenied: If the credential is missing, expired or not trusted.
        """
        ...


@runtime_checkable
class TokenExchanger(Protocol):
    """Obtains a token that lets this service act for a caller at another service.

    An agent never forwards the credential it was shown. It exchanges the
    caller's verified identity for a token bound to one audience, and presents
    that token to the MCP server with that audience.
    """

    async def exchange(self, context: RequestContext, audience: str) -> SecretStr:
        """Return a token for ``context.principal`` that only ``audience`` accepts.

        Raises:
            PolicyDenied: If no token may be issued for this caller and audience.
        """
        ...

    async def service_token(self, audience: str) -> SecretStr:
        """Return a token for this service itself that only ``audience`` accepts.

        It names no person. A service presents it where it acts in its own
        name, for example when an agent asks an MCP server which tools it has.

        Raises:
            PolicyDenied: If no token may be issued for this service and audience.
        """
        ...


@runtime_checkable
class TokenAuthenticator(Protocol):
    """Optional: an identity verifier whose credentials are tokens from a named issuer.

    Such a verifier can answer a narrower question than ``verify``: is this a
    valid token for this service, whoever it names? A server asks it at its
    door, for every request, before it asks who may do what.
    """

    @property
    def issuer(self) -> str:
        """The issuer whose tokens are accepted, as a URL."""
        ...

    async def authenticate(self, credential: str | None) -> Principal:
        """Return the principal a valid token names, a person or an application.

        Raises:
            PolicyDenied: If the token is missing, malformed, expired, not
                signed by the issuer, or meant for another service or tenant.
        """
        ...


@dataclass(frozen=True, slots=True)
class ModelCapabilities:
    """What a model provider can do. Read by the pipelines instead of vendor names.

    Attributes:
        tool_calling: Models can be given tools to call.
        structured_output: The provider can constrain output to a schema.
        prompt_caching: The provider can cache a stable prompt prefix.
        token_counting: The provider can count tokens before a call.
        context_window: The context size in tokens, when the provider knows it.
    """

    tool_calling: bool = False
    structured_output: bool = False
    prompt_caching: bool = False
    token_counting: bool = False
    context_window: int | None = None


class ChatModelProvider(Protocol):
    """Builds chat models for one vendor or gateway.

    The model object is the framework's own chat model type. This port does not
    name that type, so the contracts stay free of any framework.
    """

    @property
    def capabilities(self) -> ModelCapabilities:
        """What this provider can do."""
        ...

    def create(self, model_id: str) -> object:
        """Return a chat model for ``model_id``."""
        ...

    def classify_error(self, error: BaseException) -> AgentLibError | None:
        """Map a vendor error to the library's taxonomy.

        Returns:
            The error to raise in its place, or ``None`` if ``error`` is not one
            of the vendor's own.
        """
        ...


class CheckpointBackend(Protocol):
    """Holds the durable store for graph thread state.

    The checkpointer object is the agent framework's own type. This port does
    not name it, so the contracts stay free of any framework.
    """

    @property
    def checkpointer(self) -> object:
        """The framework checkpointer backed by this store."""
        ...


@runtime_checkable
class SupportsValidation(Protocol):
    """Optional: an adapter that can check its own readiness at startup."""

    async def validate(self) -> None:
        """Raise ``ConfigurationError`` if the adapter cannot do its job."""
        ...


@runtime_checkable
class SupportsAsyncClose(Protocol):
    """Optional: an adapter that holds resources to release at shutdown."""

    async def aclose(self) -> None:
        """Release resources. Must be safe to call more than once."""
        ...
