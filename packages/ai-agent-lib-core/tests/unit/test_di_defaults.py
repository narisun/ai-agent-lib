"""The default registry wires the local adapters to their sections."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr

from ai_agent_lib_core.adapters import (
    EnvSecretsProvider,
    JsonlAuditSink,
    NoCheckpointBackend,
    NullTelemetry,
    OpenTelemetryTelemetry,
    StaticIdentityVerifier,
)
from ai_agent_lib_core.contracts import (
    ConfigurationError,
    DeploymentEnv,
    ProviderSelection,
    Section,
    ServiceConfig,
)
from ai_agent_lib_core.di import DATA_PORT, MODEL_PORT, ServiceContainer, ServiceProviders
from ai_agent_lib_core.testing import RecordingTelemetry
from ai_agent_lib_core.testing.contracts import make_audit_record


def local_config(tmp_path: Path, **audit_options: object) -> ServiceConfig:
    return ServiceConfig.for_testing(
        secrets={"anthropic_api_key": SecretStr("sk-secret")},
        sections={
            Section.SECRETS: ProviderSelection("env"),
            Section.AUDIT: ProviderSelection(
                "jsonl", {"path": str(tmp_path / "audit.jsonl"), **audit_options}
            ),
            Section.IDENTITY: ProviderSelection("static", {"tenant": "acme"}),
        },
    )


def registry() -> ServiceProviders:
    providers = ServiceProviders.default()
    for section in Section:
        providers.register(section, "fake", lambda context: object())
    return providers


def test_the_default_registry_marks_development_adapters_local_only() -> None:
    providers = ServiceProviders.default()
    assert providers.lookup(Section.AUDIT, "jsonl").local_only is True
    assert providers.lookup(Section.IDENTITY, "static").local_only is True
    assert providers.lookup(Section.SECRETS, "env").local_only is False


async def test_local_adapters_are_built_from_their_own_sections(tmp_path: Path) -> None:
    async with ServiceContainer(local_config(tmp_path), registry()) as services:
        await services.validate()
        assert isinstance(services.audit, JsonlAuditSink)
        assert isinstance(services.secrets, EnvSecretsProvider)
        assert isinstance(services.identity, StaticIdentityVerifier)

        secret = await services.secrets.get_secret("anthropic_api_key")
        assert secret.get_secret_value() == "sk-secret"
        assert (await services.identity.verify(None)).tenant == "acme"
        await services.audit.write(make_audit_record())
    assert (tmp_path / "audit.jsonl").read_text(encoding="utf-8").count("\n") == 1


async def test_a_mistyped_adapter_option_stops_startup(tmp_path: Path) -> None:
    config = local_config(tmp_path, fsynk=True)
    with pytest.raises(ConfigurationError) as caught:
        await ServiceContainer(config, registry()).start()
    error = caught.value
    assert error.message == "the options of provider 'jsonl' are not valid (1 problem)"
    assert error.expected == "only known options: fsync, path, tracing"
    assert error.actual == "an unknown option 'fsynk' (did you mean 'fsync'?)"
    # Which variable to correct is named on the way out of the container.
    assert any("EAP_AUDIT_OPTIONS" in note for note in error.__notes__)


async def test_a_wrong_option_value_says_what_was_expected_and_what_was_given(
    tmp_path: Path,
) -> None:
    config = local_config(tmp_path, fsync="yes please", path=["a", "b"])
    with pytest.raises(ConfigurationError) as caught:
        await ServiceContainer(config, registry()).start()
    error = caught.value
    assert error.expected is not None
    assert "fsync: a valid boolean" in error.expected
    assert error.actual is not None
    assert "fsync = 'yes please'" in error.actual
    # A value that is not short and plain is described, not shown.
    assert "path = a list of 2 items" in error.actual


async def test_development_adapters_cannot_start_in_production(tmp_path: Path) -> None:
    config = ServiceConfig.for_testing(
        deployment_env=DeploymentEnv.PROD, sections=local_config(tmp_path).sections
    )
    with pytest.raises(ConfigurationError, match="local development only"):
        await ServiceContainer(config, registry()).start()


def test_telemetry_follows_the_audit_tracing_option(tmp_path: Path) -> None:
    off = ServiceContainer(local_config(tmp_path), registry())
    on = ServiceContainer(local_config(tmp_path, tracing=True), registry())
    assert isinstance(off.telemetry, NullTelemetry)
    assert isinstance(on.telemetry, OpenTelemetryTelemetry)
    with pytest.raises(ConfigurationError, match="tracing"):
        ServiceContainer(local_config(tmp_path, tracing="yes"), registry())


def test_telemetry_can_be_injected(tmp_path: Path) -> None:
    telemetry = RecordingTelemetry()
    services = ServiceContainer(local_config(tmp_path), registry(), telemetry=telemetry)
    assert services.telemetry is telemetry


async def test_a_service_that_keeps_no_graph_state_selects_no_checkpoint_store(
    tmp_path: Path,
) -> None:
    config = local_config(tmp_path).with_section(Section.CHECKPOINT, ProviderSelection("none"))
    async with ServiceContainer(config, registry()) as services:
        await services.validate()
        assert isinstance(services.checkpoint_backend, NoCheckpointBackend)
        # Compiling a graph that keeps state is then a configuration mistake, said plainly.
        with pytest.raises(ConfigurationError, match="checkpoint provider is 'none'"):
            services.compile_kwargs()
    assert ServiceProviders.default().lookup(Section.CHECKPOINT, "none").local_only is False


async def test_a_wrong_tracing_switch_names_the_value_and_the_variable(tmp_path: Path) -> None:
    config = local_config(tmp_path, tracing="x" * 60)
    with pytest.raises(ConfigurationError) as caught:
        ServiceContainer(config, registry())
    error = caught.value
    # A long value could be a credential pasted in the wrong place: it is described.
    assert (error.expected, error.actual) == ("true or false", "text of 60 characters")
    assert error.fix is not None
    assert "EAP_AUDIT_OPTIONS" in error.fix


def test_every_adapter_that_ships_declares_its_options() -> None:
    providers = ServiceProviders.default()
    ports = [MODEL_PORT, *(section.value for section in Section), DATA_PORT]
    undeclared = [
        f"{port}/{name}"
        for port in ports
        for name in providers.names(port)
        if providers.lookup(port, name).options is None
    ]
    assert undeclared == []


def test_every_adapter_that_ships_says_what_it_needs_from_the_cloud() -> None:
    # A deployment grants a service only what its adapters declare, so none may stay silent.
    providers = ServiceProviders.default()
    ports = [MODEL_PORT, *(section.value for section in Section), DATA_PORT]
    silent = [
        f"{port}/{name}"
        for port in ports
        for name in providers.names(port)
        if providers.lookup(port, name).access is None
    ]
    assert silent == []


async def test_every_options_problem_is_reported_before_anything_is_built(
    tmp_path: Path,
) -> None:
    config = ServiceConfig.for_testing(
        sections={
            Section.SECRETS: ProviderSelection("env", {"prefix": "x"}),
            Section.AUDIT: ProviderSelection("jsonl", {"fsynk": True}),
        },
    )
    built: list[str] = []

    def watched(context: object) -> object:
        built.append("built")
        return object()

    providers = registry()
    providers.register(Section.IDENTITY, "fake", watched, replace=True)
    with pytest.raises(ConfigurationError) as caught:
        await ServiceContainer(config, providers).start()
    error = caught.value
    assert error.message == "the options of 2 adapters are not valid"
    assert error.actual is not None
    assert "an unknown option 'prefix'" in error.actual
    assert "EAP_SECRETS_OPTIONS" in error.actual
    assert "did you mean 'fsync'?" in error.actual
    assert built == []
