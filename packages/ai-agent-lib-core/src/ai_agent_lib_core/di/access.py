"""Derive what a service needs from its cloud, from the configuration it runs with.

Every adapter the configuration selects is asked what it needs. The answer is
a plan a deployment turns into permissions, so a service is granted what its
adapters call and nothing else.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from ai_agent_lib_core.contracts import (
    Access,
    AccessQuery,
    ConfigurationError,
    ProviderSelection,
    ServiceConfig,
)
from ai_agent_lib_core.di.providers import DATA_PORT, MODEL_PORT, ServiceProviders

__all__ = ["AccessPlan", "AdapterAccess", "access_plan"]


@dataclass(frozen=True, slots=True)
class AdapterAccess:
    """What one selected adapter needs.

    Attributes:
        port: The port it is selected for.
        name: The adapter's name.
        instance: The data source name, or the empty string.
        access: What it needs from the cloud. Empty when it needs nothing.
        declared: Whether the adapter says what it needs. When it does not,
            the deployment cannot know, and the plan says so.
        local_only: Whether the adapter may run only in local development.
    """

    port: str
    name: str
    instance: str = ""
    access: tuple[Access, ...] = ()
    declared: bool = True
    local_only: bool = False

    @property
    def label(self) -> str:
        """``port adapter`` or ``data source name (adapter)``, for a reader."""
        if self.instance:
            return f"data source {self.instance} ({self.name})"
        return f"{self.port} {self.name}"


@dataclass(frozen=True, slots=True)
class AccessPlan:
    """What every adapter of one service needs.

    Attributes:
        adapters: One entry per selected adapter, in a stable order.
    """

    adapters: tuple[AdapterAccess, ...] = field(default=())

    @property
    def access(self) -> tuple[Access, ...]:
        """Every access, in order, with exact repeats removed."""
        seen: dict[Access, None] = {}
        for adapter in self.adapters:
            for access in adapter.access:
                seen.setdefault(access, None)
        return tuple(seen)

    @property
    def undeclared(self) -> tuple[AdapterAccess, ...]:
        """The adapters that do not say what they need."""
        return tuple(adapter for adapter in self.adapters if not adapter.declared)

    @property
    def local_only(self) -> tuple[AdapterAccess, ...]:
        """The adapters that may not be deployed at all."""
        return tuple(adapter for adapter in self.adapters if adapter.local_only)


def _models_by_provider(config: ServiceConfig) -> dict[str, list[str]]:
    models: dict[str, list[str]] = defaultdict(list)
    models[config.model.provider]
    if config.model.model_id is not None:
        models[config.model.provider].append(config.model.model_id)
    for ref in config.model.aliases.values():
        if ref.model_id not in models[ref.provider]:
            models[ref.provider].append(ref.model_id)
    return models


def access_plan(config: ServiceConfig, providers: ServiceProviders | None = None) -> AccessPlan:
    """Ask every adapter ``config`` selects what it needs from the cloud.

    Nothing is built and nothing is called: each adapter answers from its
    options alone.

    Args:
        config: The configuration the service will run with.
        providers: The adapters to look the selections up in. By default the
            ones a service gets without asking.

    Raises:
        ConfigurationError: If the configuration selects an adapter that is
            not registered, or an adapter rejects its options.
    """
    registry = providers if providers is not None else ServiceProviders.default()
    wanted: list[tuple[str, ProviderSelection, AccessQuery]] = []
    for provider, model_ids in _models_by_provider(config).items():
        selection = ProviderSelection(provider=provider)
        wanted.append((MODEL_PORT, selection, AccessQuery(selection, tuple(model_ids))))
    for section, selection in config.sections.items():
        wanted.append((str(section), selection, AccessQuery(selection)))
    for name, selection in sorted(config.data_sources.items()):
        wanted.append((DATA_PORT, selection, AccessQuery(selection, instance=name)))

    adapters = []
    for port, selection, query in wanted:
        spec = registry.lookup(port, selection.provider)
        if spec.access is None:
            adapters.append(
                AdapterAccess(
                    port=port,
                    name=spec.name,
                    instance=query.instance,
                    declared=False,
                    local_only=spec.local_only,
                )
            )
            continue
        try:
            # A rule parses its own options; a bad one raises ConfigurationError.
            needed = tuple(spec.access(query))
        except ConfigurationError as error:
            where = f"data source {query.instance}" if query.instance else f"the {port} adapter"
            error.add_note(f"while working out what {where} ({spec.name}) needs from the cloud")
            raise
        adapters.append(
            AdapterAccess(
                port=port,
                name=spec.name,
                instance=query.instance,
                access=needed,
                local_only=spec.local_only,
            )
        )
    return AccessPlan(tuple(adapters))
