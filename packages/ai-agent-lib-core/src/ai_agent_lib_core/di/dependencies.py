"""Validate and order selected providers before any factory is called."""

from collections.abc import Sequence

from ai_agent_lib_core.contracts import ConfigurationError
from ai_agent_lib_core.di.providers import ServiceProviders

AdapterKey = tuple[str, str, str]
"""Port, provider name, and instance name; an empty instance denotes a singleton."""


def build_order(selected: Sequence[AdapterKey], providers: ServiceProviders) -> list[AdapterKey]:
    """Place each dependency before its dependent, preserving other selection order.

    This inspects registry definitions without calling factories. Dependencies
    must identify a single selected singleton, not a named data-source instance.

    Raises:
        ConfigurationError: If a provider is unregistered, a dependency is
            missing or ambiguous, or dependencies form a cycle.
    """
    ordered: list[AdapterKey] = []
    visiting: list[AdapterKey] = []

    def visit(key: AdapterKey) -> None:
        if key in ordered:
            return
        if key in visiting:
            cycle = " -> ".join(k[0] for k in [*visiting, key])
            raise ConfigurationError(f"adapters depend on each other in a cycle: {cycle}")
        visiting.append(key)
        spec = providers.lookup(key[0], key[1])
        for dependency in spec.dependencies or ():
            matches = [candidate for candidate in selected if candidate[0] == dependency]
            if len(matches) != 1 or matches[0][2]:
                raise ConfigurationError(
                    f"{spec.port} ({spec.name}) requires one selected singleton {dependency} "
                    f"dependency; found {len(matches)}"
                )
            visit(matches[0])
        visiting.pop()
        ordered.append(key)

    for key in selected:
        visit(key)
    return ordered
