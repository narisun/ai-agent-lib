"""A ready-made set of fakes and the container that uses them."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from ai_agent_lib_core.adapters import FakeChatModelProvider, ScriptedResponse
from ai_agent_lib_core.contracts import (
    CheckpointBackend,
    ConfigurationError,
    DataSource,
    IdentityVerifier,
    ProviderSelection,
    Section,
    ServiceConfig,
)
from ai_agent_lib_core.di import (
    DATA_PORT,
    MODEL_PORT,
    BuildContext,
    Factory,
    ServiceContainer,
    ServiceProviders,
)
from ai_agent_lib_core.integrations.langgraph import InMemoryCheckpointBackend
from ai_agent_lib_core.testing.fakes import (
    FakeGuardrails,
    FakeIdentityVerifier,
    FakePolicyDecisionPoint,
    FakeRegistry,
    FakeSecretsProvider,
    FrozenClock,
    InMemoryAuditSink,
    RecordingTelemetry,
    SequentialIds,
)

__all__ = ["FAKE_PROVIDER", "Fakes", "fake_providers"]

FAKE_PROVIDER = "fake"
"""The adapter name ``ServiceConfig.for_testing()`` selects for every section."""


def _serve(service: object) -> Factory:
    """Return a factory that always hands out ``service``."""

    def factory(context: BuildContext) -> object:  # noqa: ARG001 - a fake needs no options
        return service

    return factory


@dataclass
class Fakes:
    """One fake per port, kept together so a test can inspect them afterwards.

    Example::

        fakes = Fakes(model=FakeChatModelProvider(["hello"]))
        async with fakes.container() as services:
            ...
        assert fakes.audit.records[0].event == "model.call"

    Attributes:
        data_sources: Fake data sources by name. Each is served under that name
            by ``services.data_source(name)``.
        mcp_connector: Opens connections to MCP servers, for example an
            ``InProcessMcpConnector`` from ``ai_agent_lib_core.testing.mcp``.
    """

    audit: InMemoryAuditSink = field(default_factory=InMemoryAuditSink)
    secrets: FakeSecretsProvider = field(default_factory=FakeSecretsProvider)
    identity: IdentityVerifier = field(default_factory=FakeIdentityVerifier)
    checkpoint: CheckpointBackend = field(default_factory=InMemoryCheckpointBackend)
    registry: FakeRegistry = field(default_factory=FakeRegistry)
    policy: FakePolicyDecisionPoint = field(default_factory=FakePolicyDecisionPoint)
    guardrails: FakeGuardrails = field(default_factory=FakeGuardrails)
    model: FakeChatModelProvider = field(default_factory=FakeChatModelProvider)
    clock: FrozenClock = field(default_factory=FrozenClock)
    ids: SequentialIds = field(default_factory=SequentialIds)
    telemetry: RecordingTelemetry = field(default_factory=RecordingTelemetry)
    mcp_connector: Any = None
    data_sources: dict[str, DataSource] = field(default_factory=dict)

    def providers(self) -> ServiceProviders:
        """Return a registry that serves these fakes under the name ``fake``."""
        return (
            ServiceProviders()
            .register(Section.SECRETS, FAKE_PROVIDER, _serve(self.secrets))
            .register(Section.AUDIT, FAKE_PROVIDER, _serve(self.audit))
            .register(Section.IDENTITY, FAKE_PROVIDER, _serve(self.identity))
            .register(Section.CHECKPOINT, FAKE_PROVIDER, _serve(self.checkpoint))
            .register(Section.REGISTRY, FAKE_PROVIDER, _serve(self.registry))
            .register(Section.POLICY, FAKE_PROVIDER, _serve(self.policy))
            .register(Section.GUARDRAILS, FAKE_PROVIDER, _serve(self.guardrails))
            .register(MODEL_PORT, FAKE_PROVIDER, _serve(self.model))
            .register(DATA_PORT, FAKE_PROVIDER, self._data_source)
        )

    def _data_source(self, context: BuildContext) -> DataSource:
        try:
            return self.data_sources[context.instance]
        except KeyError:
            raise ConfigurationError(
                f"no fake data source called {context.instance!r}; add it to Fakes.data_sources"
            ) from None

    def config(self) -> ServiceConfig:
        """Return a test configuration that selects every fake, data sources included."""
        return ServiceConfig.for_testing(
            data_sources={
                name: ProviderSelection(provider=FAKE_PROVIDER) for name in self.data_sources
            }
        )

    def container(self, config: ServiceConfig | None = None) -> ServiceContainer:
        """Return a container wired to these fakes. Use it with ``async with``."""
        return ServiceContainer(
            config if config is not None else self.config(),
            self.providers(),
            clock=self.clock,
            ids=self.ids,
            telemetry=self.telemetry,
            mcp_connector=self.mcp_connector,
        )


def fake_providers(
    model: FakeChatModelProvider | Sequence[ScriptedResponse] | None = None,
) -> ServiceProviders:
    """Return a registry of fakes, optionally with a scripted model.

    Args:
        model: A fake model provider, or just the script for one.
    """
    if model is None or isinstance(model, FakeChatModelProvider):
        provider = model if model is not None else FakeChatModelProvider()
    else:
        provider = FakeChatModelProvider(model)
    return Fakes(model=provider).providers()
