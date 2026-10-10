"""The provider registry: which adapters exist and how to build them.

Configuration selects an adapter by name. This registry maps the name to a
factory written in code, so a configuration value can never name an import
path or load code of its own.
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path
from types import MappingProxyType
from typing import Any, Generic, TypeVar, cast, overload

from pydantic import SecretStr

from ai_agent_lib_core.contracts import (
    AccessRule,
    Clock,
    ConfigurationError,
    DeploymentEnv,
    ExternalSettings,
    IdGenerator,
    OptionsModel,
    ProviderSelection,
    describe,
)

__all__ = [
    "DATA_PORT",
    "ENTRY_POINT_GROUP",
    "FIRST_PARTY_PACKS",
    "MODEL_PORT",
    "BuildContext",
    "BuildResources",
    "Factory",
    "ProviderSpec",
    "ServicePort",
    "ServiceProviders",
]

MODEL_PORT = "model"
"""Port name under which chat model providers are registered."""

DATA_PORT = "data"
"""Port name under which data source adapters are registered."""

ENTRY_POINT_GROUP = "ai_agent_lib.providers"
"""Entry-point group through which other distributions offer provider packs."""

FIRST_PARTY_PACKS = ("ai-agent-lib-aws",)
"""Distributions of this library whose provider packs load whenever they are installed.

Installing one of them is the decision to use it, so moving a service to a
server-side adapter is a change of configuration and never a change of code.
A pack from any other distribution loads only when the service names it.
"""

# Adapters that live in another distribution, so a missing one can name its fix.
_INSTALL_HINTS: Mapping[tuple[str, str], str] = MappingProxyType(
    {
        (MODEL_PORT, "bedrock"): "ai-agent-lib-aws",
        (DATA_PORT, "redshift_data"): "ai-agent-lib-aws",
        ("secrets", "secrets_manager"): "ai-agent-lib-aws",
        ("audit", "firehose"): "ai-agent-lib-aws",
        ("checkpoint", "postgres"): "ai-agent-lib-aws",
        ("registry", "s3_file"): "ai-agent-lib-aws",
        ("guardrails", "bedrock"): "ai-agent-lib-aws",
    }
)


def _no_dependencies(port: str) -> object:
    raise ConfigurationError(f"{port} is not available to this factory")


ResourceT = TypeVar("ResourceT")


@dataclass(frozen=True, slots=True)
class ServicePort(Generic[ResourceT]):
    """A typed port with the member names required at its runtime boundary.

    Registering and resolving the same descriptor preserves the adapter type
    for static checkers. Runtime checks reject missing members before any
    dependent factory runs. Framework-specific behavior belongs in contract tests.

    Attributes:
        name: Configuration-facing port name, such as ``"audit"``.
        members: Attributes that an adapter must expose.
        methods: Members that must also be callable. Signatures and behavior
            remain the responsibility of static checking and contract tests.
    """

    name: str
    members: tuple[str, ...]
    methods: tuple[str, ...] = ()

    def checked(self, value: object) -> ResourceT:
        """Reject an adapter that does not expose this port's members."""
        absent = object()
        missing = [
            name for name in self.members if inspect.getattr_static(value, name, absent) is absent
        ]
        if missing:
            raise ConfigurationError(
                f"adapter for {self.name} is missing required members: {', '.join(missing)}"
            )
        invalid = [
            name for name in self.methods if not callable(inspect.getattr_static(value, name, None))
        ]
        if invalid:
            raise ConfigurationError(
                f"adapter for {self.name} has non-callable methods: {', '.join(invalid)}"
            )
        return cast(ResourceT, value)


class BuildResources:
    """Shared resources owned by one container, never by a provider registry.

    Factories use a stable key to share a resource within their container.
    The container closes owned resources after closing the adapters that use them.
    """

    def __init__(self) -> None:
        self._values: dict[object, object] = {}

    def shared(self, key: object, create: Callable[[], ResourceT]) -> ResourceT:
        """Return the resource for ``key``, creating it on first use.

        A key must always refer to the same resource type. Creation is synchronous
        during sequential adapter startup; this is not a thread-safe cache.
        A failing creator must clean up resources it has not returned.
        """
        if key not in self._values:
            self._values[key] = create()
        return cast(ResourceT, self._values[key])

    def take(self) -> tuple[object, ...]:
        """Transfer resources to the container's teardown, in reverse creation order."""
        values = tuple(reversed(tuple(self._values.values())))
        self._values.clear()
        return values


@dataclass(frozen=True, slots=True)
class BuildContext:
    """What a factory is given to build its adapter.

    A factory sees its own section and a few shared settings. It never sees the
    environment, a variable name or another adapter's options.

    Attributes:
        port: The port the adapter is being built for.
        selection: The adapter's own name and raw options.
        deployment_env: Where the process is running.
        tls_ca_bundle: The enterprise CA file, if one is configured.
        external: Values read from third-party variables.
        clock: The clock port.
        ids: The identifier port.
        secret_values: Named secret values. Populated only for the secrets port.
        resolver: Resolves already-built singleton dependencies. Factories
            normally call ``get`` rather than accessing this callback directly.
        instance: The name of the thing being built, for ports that can have
            several adapters at once, such as data sources. Empty otherwise.
        resources: Resources shared and owned within this container, such as
            an AWS session factory. They close after the adapters that use them.
    """

    port: str
    selection: ProviderSelection
    deployment_env: DeploymentEnv
    tls_ca_bundle: Path | None
    external: ExternalSettings
    clock: Clock
    ids: IdGenerator
    secret_values: Mapping[str, SecretStr] = field(default_factory=dict, repr=False)
    resolver: Callable[[str], object] = field(default=_no_dependencies, repr=False)
    instance: str = ""
    resources: BuildResources = field(default_factory=BuildResources, repr=False)

    @overload
    def get(self, port: ServicePort[ResourceT]) -> ResourceT: ...

    @overload
    def get(self, port: str) -> object: ...

    def get(self, port: str | ServicePort[Any]) -> object:
        """Return an already-built singleton dependency.

        Declared dependencies restrict which ports are accessible. A typed
        descriptor also checks the returned adapter's required members.
        This method never constructs an adapter on demand.
        """
        if isinstance(port, ServicePort):
            return port.checked(self.resolver(port.name))
        return self.resolver(port)


Factory = Callable[[BuildContext], object | Awaitable[object]]
"""Build an adapter synchronously or asynchronously from a ``BuildContext``.

The container takes ownership of the returned adapter and calls ``aclose`` if
supported. Construction and close run in the same lifetime task, allowing
task-bound async context managers. A factory that fails before returning must
clean up its own partially constructed adapter.
"""


@dataclass(frozen=True, slots=True)
class ProviderSpec:
    """The definition of an adapter, not a live instance.

    Attributes:
        port: The port it implements.
        name: The name configuration uses to select it.
        factory: Builds the adapter.
        local_only: Whether it may be used only in local development.
        options: The model its options must satisfy, so that they can be
            checked before anything is built and documented from the code.
            :class:`~ai_agent_lib_core.contracts.NoOptions` for an adapter that
            takes none; ``None`` when the adapter does not say.
        access: What the adapter needs from the cloud it runs in, given its
            options, so a deployment grants only that.
            :func:`~ai_agent_lib_core.contracts.no_access` for an adapter that
            needs nothing; ``None`` when the adapter does not say.
        extra: The optional extra of its distribution that it needs installed,
            such as ``"bedrock"`` for ``ai-agent-lib-aws[bedrock]``.
        dependencies: Singleton ports to build first and allow through ``get``.
            ``()`` declares none; ``None`` preserves legacy ordering and access
            to any already-built singleton.
        contract: Optional typed descriptor used to check a factory's result.
    """

    port: str
    name: str
    factory: Factory
    local_only: bool = False
    options: type[OptionsModel] | None = None
    access: AccessRule | None = None
    extra: str | None = None
    dependencies: tuple[str, ...] | None = None
    contract: ServicePort[Any] | None = None


def _normalize(distribution: str) -> str:
    return re.sub(r"[-_.]+", "-", distribution).lower()


class ServiceProviders:
    """A registry from ``(port, name)`` to an adapter factory.

    The registry can be changed until a container starts with it. From then on
    it is frozen. A registry can be reused by multiple containers: it holds
    definitions, while each container owns the resources its factories create.
    """

    def __init__(self) -> None:
        self._specs: dict[tuple[str, str], ProviderSpec] = {}
        self._frozen = False

    @classmethod
    def default(cls) -> ServiceProviders:
        """Return a registry holding the adapters a service gets without asking.

        These are the local adapters that ship with core, and the adapters of
        the library's own provider packs that are installed, such as
        ``ai-agent-lib-aws``.
        """
        from ai_agent_lib_core.di.defaults import register_local_adapters

        providers = cls()
        register_local_adapters(providers)
        return providers.with_installed(FIRST_PARTY_PACKS)

    @property
    def frozen(self) -> bool:
        """Whether registrations are frozen, explicitly or by container startup."""
        return self._frozen

    def freeze(self) -> None:
        """Stop further changes. Called by the container when it starts."""
        self._frozen = True

    @overload
    def register(
        self,
        port: ServicePort[ResourceT],
        name: str,
        factory: Callable[[BuildContext], ResourceT | Awaitable[ResourceT]],
        *,
        local_only: bool = False,
        options: type[OptionsModel] | None = None,
        access: AccessRule | None = None,
        extra: str | None = None,
        replace: bool = False,
        dependencies: Iterable[str | ServicePort[Any]] | None = None,
    ) -> ServiceProviders: ...

    @overload
    def register(
        self,
        port: str,
        name: str,
        factory: Factory,
        *,
        local_only: bool = False,
        options: type[OptionsModel] | None = None,
        access: AccessRule | None = None,
        extra: str | None = None,
        replace: bool = False,
        dependencies: Iterable[str | ServicePort[Any]] | None = None,
    ) -> ServiceProviders: ...

    def register(
        self,
        port: str | ServicePort[Any],
        name: str,
        factory: Factory,
        *,
        local_only: bool = False,
        options: type[OptionsModel] | None = None,
        access: AccessRule | None = None,
        extra: str | None = None,
        replace: bool = False,
        dependencies: Iterable[str | ServicePort[Any]] | None = None,
    ) -> ServiceProviders:
        """Add an adapter and return the registry, so calls can be chained.

        Args:
            port: The port the adapter implements.
            name: The name configuration uses to select it.
            factory: Builds the adapter.
            local_only: Whether it may be used only in local development.
            options: The options model the factory parses, checked at startup
                before anything is built. ``NoOptions`` for none.
            access: What the adapter needs from the cloud, given its options.
                ``no_access`` for nothing.
            extra: The optional extra of its distribution that it needs.
            replace: Allow overwriting an existing registration.
            dependencies: Singleton ports needed by this factory, built first.
                ``None`` preserves legacy section ordering and access to any
                already-built singleton.
                An explicit empty iterable declares no dependencies.

        Raises:
            RuntimeError: If the registry is frozen.
            ValueError: If the name is taken and ``replace`` is false.
        """
        if self._frozen:
            raise RuntimeError(
                "the provider registry is frozen; register before the container starts"
            )
        contract = port if isinstance(port, ServicePort) else None
        port_name = contract.name if contract is not None else str(port)
        key = (port_name, name)
        if key in self._specs and not replace:
            raise ValueError(f"provider {name!r} is already registered for {port}")
        self._specs[key] = ProviderSpec(
            port=port_name,
            name=name,
            factory=factory,
            local_only=local_only,
            options=options,
            access=access,
            extra=extra,
            dependencies=(
                tuple(p.name if isinstance(p, ServicePort) else str(p) for p in dependencies)
                if dependencies is not None
                else None
            ),
            contract=contract,
        )
        return self

    def lookup(self, port: str, name: str) -> ProviderSpec:
        """Return an adapter definition without constructing an instance.

        Raises:
            ConfigurationError: If no such adapter is registered.
        """
        key = (str(port), name)
        spec = self._specs.get(key)
        if spec is not None:
            return spec
        hint = _INSTALL_HINTS.get(key)
        if hint is not None:
            raise ConfigurationError(
                f"provider {name!r} for {port} is not available",
                expected=f"the {hint} distribution, which provides it, installed",
                actual=f"{hint} is not installed",
                fix=f"add \"{hint}\" to the service's dependencies, or 'pip install {hint}'",
            )
        known = ", ".join(self.names(port)) or "none"
        raise ConfigurationError(
            f"provider {name!r} for {port} is not registered; registered providers: {known}"
        )

    def names(self, port: str) -> tuple[str, ...]:
        """Return the adapter names registered for ``port``, sorted."""
        return tuple(sorted(name for (p, name) in self._specs if p == str(port)))

    def with_installed(self, allow: Iterable[str]) -> ServiceProviders:
        """Add provider packs offered by installed distributions on the allowlist.

        A distribution offers a pack through the ``ai_agent_lib.providers`` entry
        point group. Each entry point names a function that takes this registry
        and registers its adapters. Packs from distributions that are not named
        in ``allow`` are ignored, however they were installed.

        Args:
            allow: Distribution names that are trusted to add adapters.

        Raises:
            ConfigurationError: If an allowed pack cannot be loaded.
        """
        allowed = {_normalize(name) for name in allow}
        for entry_point in metadata.entry_points(group=ENTRY_POINT_GROUP):
            distribution = entry_point.dist.name if entry_point.dist is not None else ""
            if _normalize(distribution) not in allowed:
                continue
            try:
                register_pack = entry_point.load()
            except Exception as exc:
                raise ConfigurationError(
                    f"the provider pack of {distribution} could not be loaded ({describe(exc)})"
                ) from exc
            register_pack(self)
        return self
