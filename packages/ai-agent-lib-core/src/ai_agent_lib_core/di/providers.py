"""The provider registry: which adapters exist and how to build them.

Configuration selects an adapter by name. This registry maps the name to a
factory written in code, so a configuration value can never name an import
path or load code of its own.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path
from types import MappingProxyType

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
    "Factory",
    "ProviderSpec",
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
        instance: The name of the thing being built, for ports that can have
            several adapters at once, such as data sources. Empty otherwise.
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

    def get(self, port: str) -> object:
        """Return the service behind another port this adapter depends on."""
        return self.resolver(port)


Factory = Callable[[BuildContext], object | Awaitable[object]]
"""Builds an adapter. May be a plain function or a coroutine function."""


@dataclass(frozen=True, slots=True)
class ProviderSpec:
    """One registered adapter.

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
    """

    port: str
    name: str
    factory: Factory
    local_only: bool = False
    options: type[OptionsModel] | None = None
    access: AccessRule | None = None
    extra: str | None = None


def _normalize(distribution: str) -> str:
    return re.sub(r"[-_.]+", "-", distribution).lower()


class ServiceProviders:
    """A registry from ``(port, name)`` to an adapter factory.

    The registry can be changed until a container starts with it. From then on
    it is frozen, so the set of adapters a process can use is fixed at startup.
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
        """Whether a container has started with this registry."""
        return self._frozen

    def freeze(self) -> None:
        """Stop further changes. Called by the container when it starts."""
        self._frozen = True

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

        Raises:
            RuntimeError: If the registry is frozen.
            ValueError: If the name is taken and ``replace`` is false.
        """
        if self._frozen:
            raise RuntimeError(
                "the provider registry is frozen; register before the container starts"
            )
        key = (str(port), name)
        if key in self._specs and not replace:
            raise ValueError(f"provider {name!r} is already registered for {port}")
        self._specs[key] = ProviderSpec(
            port=str(port),
            name=name,
            factory=factory,
            local_only=local_only,
            options=options,
            access=access,
            extra=extra,
        )
        return self

    def lookup(self, port: str, name: str) -> ProviderSpec:
        """Return the registered adapter, or explain how to get it.

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
