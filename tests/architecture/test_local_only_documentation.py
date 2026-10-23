"""The configuration reference names the local-only adapters the registry actually guards.

``config`` cannot import ``di``, so the binding table cannot ask the provider
registry which adapters the one-way guard refuses outside local development. It
keeps its own list instead. These tests are the bridge: they fail when a
registration gains or loses ``local_only``, or when the prose stops saying so.
"""

from __future__ import annotations

import re

import pytest

from agentcraft2_core.config import DEFAULT_BINDINGS, Key, provider_key, render_reference
from agentcraft2_core.config.bindings import LOCAL_ONLY_ADAPTERS, Binding
from agentcraft2_core.contracts import Section
from agentcraft2_core.di import ServiceProviders, ports
from agentcraft2_core.di.providers import DATA_PORT, MODEL_PORT

_PORTS = (MODEL_PORT, *(section.value for section in Section), DATA_PORT)


def _binding(key: str) -> Binding:
    return next(binding for binding in DEFAULT_BINDINGS if binding.key == key)


def _selecting_binding(port: str) -> Binding:
    """The binding whose reference text explains how to choose an adapter for ``port``."""
    if port == MODEL_PORT:
        return _binding(Key.MODEL_PROVIDER)
    if port == DATA_PORT:
        return _binding(Key.DATA_SOURCES)
    return _binding(provider_key(Section(port)))


def _registered_local_only() -> set[tuple[str, str]]:
    providers = ServiceProviders.default()
    return {
        (port, name)
        for port in _PORTS
        for name in providers.names(port)
        if providers.lookup(port, name).local_only
    }


def test_the_port_list_covers_every_public_port() -> None:
    assert {getattr(ports, name).name for name in ports.__all__} == set(_PORTS)


def test_the_documented_list_is_exactly_the_registered_local_only_adapters() -> None:
    assert set(LOCAL_ONLY_ADAPTERS) == _registered_local_only()


def test_the_deployment_env_reference_lists_every_local_only_adapter() -> None:
    details = _binding(Key.DEPLOYMENT_ENV).details
    assert [phrase for phrase in LOCAL_ONLY_ADAPTERS.values() if phrase not in details] == []
    assert details in render_reference()


@pytest.mark.parametrize(("port", "name"), sorted(LOCAL_ONLY_ADAPTERS))
def test_the_setting_that_selects_a_local_only_adapter_says_it_is_local_only(
    port: str, name: str
) -> None:
    binding = _selecting_binding(port)
    assert re.search(rf"`{re.escape(name)}`[^`]*local[- ]only", binding.details), (
        f"{binding.name} does not say `{name}` is local-only"
    )
