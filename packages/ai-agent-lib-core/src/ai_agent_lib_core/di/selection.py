"""Which adapters a configuration selects, and whether each may be built.

These are the pure decisions the container makes before any factory runs:
what the configuration asks for, whether a local-only adapter is allowed
here, and whether every selected adapter's options parse. They name the
variables a developer changes, so every error says where to look.
"""

from __future__ import annotations

from collections.abc import Sequence

from ai_agent_lib_core.config import Key, options_key, provider_key, variable_for
from ai_agent_lib_core.contracts import (
    ConfigurationError,
    DeploymentEnv,
    ProviderSelection,
    Section,
    ServiceConfig,
)
from ai_agent_lib_core.di.access import selected_models
from ai_agent_lib_core.di.providers import DATA_PORT, MODEL_PORT, ProviderSpec, ServiceProviders

__all__ = [
    "AdapterKey",
    "check_allowed",
    "check_options",
    "label",
    "selected_adapters",
    "selection_for",
    "where_configured",
]

AdapterKey = tuple[str, str, str]
"""Identifies one built adapter: its port, its adapter name and its instance name."""


def selected_adapters(config: ServiceConfig) -> list[AdapterKey]:
    """Return the adapters the configuration selects, in legacy order.

    Legacy order is the order that predates declared dependencies: one adapter
    per ``Section``, in the order the ``Section`` enum lists them, then each
    selected model provider, then each data-source instance. Dependency
    ordering happens afterwards (``build_order`` keeps this order, only pulling
    a declared dependency ahead of the adapter that needs it); this is what is
    wanted, not the order in which it is built.
    """
    selected: list[AdapterKey] = [
        (section.value, config.section(section).provider, "") for section in Section
    ]
    # Model providers come from ``di/access.selected_models``, which the access
    # planner also uses, so the container builds the providers the plan grants
    # access to. A service with no model configured gets no model provider.
    selected.extend((MODEL_PORT, name, "") for name in selected_models(config))
    selected.extend(
        (DATA_PORT, selection.provider, source) for source, selection in config.data_sources.items()
    )
    return selected


def label(key: AdapterKey) -> str:
    """Name an adapter the way diagnostics and errors refer to it."""
    port, name, instance = key
    return f"{port} {instance!r} ({name})" if instance else f"{port} ({name})"


def check_allowed(spec: ProviderSpec, env: DeploymentEnv) -> None:
    """Refuse a local-only adapter outside local development: the one-way guard.

    Raises:
        ConfigurationError: If the adapter is for local development only.
    """
    if spec.local_only and env is not DeploymentEnv.LOCAL:
        raise ConfigurationError(
            f"provider {spec.name!r} for {spec.port} is for local development only "
            f"and cannot be used when the deployment environment is {env.value!r}",
            expected=f"a {spec.port} provider meant for a deployed service",
            actual=f"{spec.name!r}, which is local only",
            fix=(
                f"select a server-side provider for {spec.port} (the aws profile does), "
                f"or set {variable_for(Key.DEPLOYMENT_ENV)}=local on a developer's machine"
            ),
        )


def selection_for(config: ServiceConfig, key: AdapterKey) -> ProviderSelection:
    """Return the configured selection (provider name and options) behind ``key``."""
    port, name, instance = key
    if port == MODEL_PORT:
        return config.model.selection(name)
    if port == DATA_PORT:
        return config.data_sources[instance]
    return config.section(Section(port))


def check_options(
    config: ServiceConfig, providers: ServiceProviders, selected: Sequence[AdapterKey]
) -> None:
    """Check every selected adapter's options before building any of them.

    So a mistyped option stops startup at once, with every such problem
    listed, rather than after a database or a stream has been reached.

    Raises:
        ConfigurationError: Naming every adapter whose options are invalid.
    """
    problems: list[ConfigurationError] = []
    # Validate named model options even when that provider is not active;
    # misspellings must not silently leave caching disabled.
    keys = dict.fromkeys([*selected, *((MODEL_PORT, name, "") for name in config.model.options)])
    for key in keys:
        port, name, _ = key
        model = providers.lookup(port, name).options
        if model is None:
            continue
        try:
            selection_for(config, key).parse_options(model)
        except ConfigurationError as error:
            error.add_note(where_configured(key))
            problems.append(error)
    if len(problems) == 1:
        raise problems[0]
    if problems:
        raise ConfigurationError(
            f"the options of {len(problems)} adapters are not valid",
            actual=" | ".join(f"{error.summary} [{error.__notes__[-1]}]" for error in problems),
            fix="correct each; the developer guide lists every option with its default",
        )


def where_configured(key: AdapterKey) -> str:
    """Name the variables that chose and configured the adapter being built."""
    port, name, instance = key
    if port == MODEL_PORT:
        chosen = f"{variable_for(Key.MODEL_PROVIDER)} or {variable_for(Key.MODEL_ALIASES)}"
        return (
            f"while building the model provider {name!r}, chosen by {chosen}; "
            f"options from {variable_for(Key.MODEL_OPTIONS)}"
        )
    if port == DATA_PORT:
        return (
            f"while building the data source {instance!r} of kind {name!r}, "
            f"configured in {variable_for(Key.DATA_SOURCES)}"
        )
    section = Section(port)
    return (
        f"while building the {port} adapter {name!r}, chosen by "
        f"{variable_for(provider_key(section))} with options from "
        f"{variable_for(options_key(section))}"
    )
