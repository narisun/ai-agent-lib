"""The provider registry maps names to factories and freezes at startup."""

from __future__ import annotations

from dataclasses import dataclass
from importlib import metadata

import pytest

from ai_agent_lib_core.contracts import ConfigurationError
from ai_agent_lib_core.di import (
    ENTRY_POINT_GROUP,
    FIRST_PARTY_PACKS,
    MODEL_PORT,
    BuildContext,
    ServiceProviders,
)


def _factory(context: BuildContext) -> object:
    return object()


def test_register_and_lookup() -> None:
    providers = ServiceProviders().register("audit", "memory", _factory, local_only=True)
    spec = providers.lookup("audit", "memory")
    assert (spec.port, spec.name, spec.factory, spec.local_only) == (
        "audit",
        "memory",
        _factory,
        True,
    )
    assert providers.names("audit") == ("memory",)


def test_register_can_be_chained() -> None:
    providers = ServiceProviders().register("audit", "b", _factory).register("audit", "a", _factory)
    assert providers.names("audit") == ("a", "b")


def test_a_name_cannot_be_registered_twice_by_accident() -> None:
    providers = ServiceProviders().register("audit", "memory", _factory)
    with pytest.raises(ValueError, match="already registered"):
        providers.register("audit", "memory", _factory)
    providers.register("audit", "memory", _factory, replace=True, local_only=True)
    assert providers.lookup("audit", "memory").local_only is True


def test_an_unregistered_provider_lists_the_registered_ones() -> None:
    providers = ServiceProviders().register("audit", "jsonl", _factory)
    with pytest.raises(ConfigurationError, match=r"'kafka' for audit.*jsonl"):
        providers.lookup("audit", "kafka")
    with pytest.raises(ConfigurationError, match="registered providers: none"):
        providers.lookup("cache", "redis")


def test_a_provider_from_another_package_names_the_install_command() -> None:
    with pytest.raises(ConfigurationError, match=r"pip install ai-agent-lib-aws"):
        ServiceProviders().lookup(MODEL_PORT, "bedrock")


def test_a_frozen_registry_rejects_changes() -> None:
    assert not ServiceProviders().frozen
    providers = ServiceProviders()
    providers.freeze()
    assert providers.frozen
    with pytest.raises(RuntimeError, match="frozen"):
        providers.register("audit", "late", _factory)


@dataclass
class _Distribution:
    name: str


@dataclass
class _EntryPoint:
    dist: _Distribution | None
    port: str

    def load(self) -> object:
        def register_pack(providers: ServiceProviders) -> None:
            providers.register(self.port, "from-pack", _factory)

        return register_pack


def test_only_allowlisted_distributions_may_add_provider_packs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    offered = [
        _EntryPoint(_Distribution("ai-agent-lib-aws"), "audit"),
        _EntryPoint(_Distribution("some-other-package"), "secrets"),
        _EntryPoint(None, "identity"),
    ]

    def entry_points(*, group: str) -> list[_EntryPoint]:
        assert group == ENTRY_POINT_GROUP
        return offered

    monkeypatch.setattr(metadata, "entry_points", entry_points)
    providers = ServiceProviders().with_installed(allow=["ai_agent_lib_aws"])
    assert providers.names("audit") == ("from-pack",)
    assert providers.names("secrets") == ()
    assert providers.names("identity") == ()


def test_default_registry_can_be_built() -> None:
    assert isinstance(ServiceProviders.default(), ServiceProviders)


def test_the_librarys_own_pack_loads_whenever_it_is_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    offered = [
        _EntryPoint(_Distribution("ai-agent-lib-aws"), "audit"),
        _EntryPoint(_Distribution("some-other-package"), "secrets"),
    ]
    monkeypatch.setattr(metadata, "entry_points", lambda *, group: offered)

    providers = ServiceProviders.default()

    # Installing the first-party package is enough; nothing in the service's code names it.
    assert "from-pack" in providers.names("audit")
    assert "from-pack" not in providers.names("secrets")
    assert FIRST_PARTY_PACKS == ("ai-agent-lib-aws",)


def test_the_aws_package_offers_its_pack_through_the_entry_point() -> None:
    offered = {
        entry_point.dist.name: entry_point.value
        for entry_point in metadata.entry_points(group=ENTRY_POINT_GROUP)
        if entry_point.dist is not None
    }
    assert offered.get("ai-agent-lib-aws") == "ai_agent_lib_aws:register"


def test_a_pack_that_cannot_be_loaded_stops_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Broken(_EntryPoint):
        def load(self) -> object:
            raise ImportError("no module named boto3")

    monkeypatch.setattr(
        metadata, "entry_points", lambda *, group: [_Broken(_Distribution("ai-agent-lib-aws"), "x")]
    )
    with pytest.raises(ConfigurationError, match="ai-agent-lib-aws could not be loaded"):
        ServiceProviders.default()
