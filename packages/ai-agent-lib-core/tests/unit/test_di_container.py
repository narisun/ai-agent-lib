"""The container builds once, guards, validates and closes in reverse order."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import SecretStr

from ai_agent_lib_core.contracts import (
    ConfigurationError,
    DeploymentEnv,
    ModelRef,
    ModelSection,
    ProviderSelection,
    Section,
    ServiceConfig,
)
from ai_agent_lib_core.di import (
    DATA_PORT,
    MODEL_PORT,
    BuildContext,
    ServiceContainer,
    ServiceProviders,
)


class Recorder:
    """A fake adapter that records what happens to it."""

    def __init__(self, context: BuildContext, log: list[str], **behaviour: Any) -> None:
        self.context = context
        self.log = log
        self.behaviour = behaviour
        log.append(f"build {context.port}")

    async def validate(self) -> None:
        self.log.append(f"validate {self.context.port}")
        if "invalid" in self.behaviour:
            raise ConfigurationError(self.behaviour["invalid"])

    async def aclose(self) -> None:
        self.log.append(f"close {self.context.port}")
        if "close_error" in self.behaviour:
            raise RuntimeError(self.behaviour["close_error"])


def fake_registry(log: list[str], **behaviour: dict[str, Any]) -> ServiceProviders:
    """Register a recording fake for every section and for the fake model."""
    providers = ServiceProviders()
    for port in [*(section.value for section in Section), MODEL_PORT, DATA_PORT]:

        def factory(context: BuildContext, port: str = port) -> Recorder:
            return Recorder(context, log, **behaviour.get(port, {}))

        providers.register(port, "fake", factory)
    return providers


async def test_each_section_adapter_is_built_once_at_startup() -> None:
    log: list[str] = []
    async with ServiceContainer(ServiceConfig.for_testing(), fake_registry(log)) as services:
        assert log == [
            "build secrets",
            "build audit",
            "build identity",
            "build checkpoint",
            "build registry",
            "build policy",
            "build guardrails",
            "build model",
        ]
        assert services.get(Section.AUDIT) is services.get(Section.AUDIT)
        assert services.audit is services.get(Section.AUDIT)
        assert services.secrets is services.get(Section.SECRETS)
        assert services.identity is services.get(Section.IDENTITY)
    assert log.count("build audit") == 1


async def test_adapters_close_in_reverse_build_order() -> None:
    log: list[str] = []
    async with ServiceContainer(ServiceConfig.for_testing(), fake_registry(log)):
        log.clear()
    assert log == [
        "close model",
        "close guardrails",
        "close policy",
        "close registry",
        "close checkpoint",
        "close identity",
        "close audit",
        "close secrets",
    ]


async def test_every_adapter_is_closed_even_when_one_close_fails() -> None:
    log: list[str] = []
    providers = fake_registry(log, identity={"close_error": "socket stuck"})
    with pytest.raises(ExceptionGroup) as caught:
        async with ServiceContainer(ServiceConfig.for_testing(), providers):
            log.clear()
    assert log == [
        "close model",
        "close guardrails",
        "close policy",
        "close registry",
        "close checkpoint",
        "close identity",
        "close audit",
        "close secrets",
    ]
    assert [str(error) for error in caught.value.exceptions] == ["socket stuck"]


async def test_close_is_idempotent() -> None:
    log: list[str] = []
    services = ServiceContainer(ServiceConfig.for_testing(), fake_registry(log))
    await services.start()
    await services.aclose()
    await services.aclose()
    assert log.count("close audit") == 1


async def test_a_failed_startup_closes_what_was_already_built() -> None:
    log: list[str] = []
    providers = fake_registry(log)

    def broken(context: BuildContext) -> object:
        raise ConfigurationError("identity provider unreachable")

    providers.register(Section.IDENTITY, "fake", broken, replace=True)
    with pytest.raises(ConfigurationError, match="unreachable"):
        await ServiceContainer(ServiceConfig.for_testing(), providers).start()
    assert log == ["build secrets", "build audit", "close audit", "close secrets"]


async def test_async_factories_are_awaited() -> None:
    providers = fake_registry([])

    async def factory(context: BuildContext) -> str:
        return "built asynchronously"

    providers.register(Section.AUDIT, "fake", factory, replace=True)
    async with ServiceContainer(ServiceConfig.for_testing(), providers) as services:
        assert services.get(Section.AUDIT) == "built asynchronously"


async def test_the_registry_is_frozen_once_the_container_starts() -> None:
    providers = fake_registry([])
    async with ServiceContainer(ServiceConfig.for_testing(), providers):
        with pytest.raises(RuntimeError, match="frozen"):
            providers.register("audit", "late", lambda context: object())


async def test_a_container_starts_only_once_and_must_be_started_before_use() -> None:
    services = ServiceContainer(ServiceConfig.for_testing(), fake_registry([]))
    with pytest.raises(RuntimeError, match="not started"):
        services.get(Section.AUDIT)
    await services.start()
    with pytest.raises(RuntimeError, match="only once"):
        await services.start()
    await services.aclose()
    with pytest.raises(RuntimeError, match="closed"):
        services.get(Section.AUDIT)


async def test_an_unregistered_selection_stops_startup_before_anything_is_built() -> None:
    log: list[str] = []
    config = ServiceConfig.for_testing().with_section(Section.AUDIT, ProviderSelection("kafka"))
    with pytest.raises(ConfigurationError, match="'kafka' for audit is not registered"):
        await ServiceContainer(config, fake_registry(log)).start()
    assert log == []


# ------------------------------------------------------------- one-way guard


@pytest.mark.parametrize("env", [DeploymentEnv.DEV, DeploymentEnv.PROD])
async def test_a_local_only_adapter_is_refused_outside_local(env: DeploymentEnv) -> None:
    log: list[str] = []
    providers = fake_registry(log)
    providers.register(
        Section.CHECKPOINT, "fake", lambda c: object(), local_only=True, replace=True
    )
    config = ServiceConfig.for_testing(deployment_env=env)
    with pytest.raises(ConfigurationError) as caught:
        await ServiceContainer(config, providers).start()
    assert "local development only" in str(caught.value)
    assert env.value in str(caught.value)
    assert log == []


async def test_a_local_only_adapter_is_allowed_locally() -> None:
    providers = fake_registry([])
    providers.register(
        Section.CHECKPOINT, "fake", lambda c: "sqlite", local_only=True, replace=True
    )
    async with ServiceContainer(ServiceConfig.for_testing(), providers) as services:
        assert services.get(Section.CHECKPOINT) == "sqlite"


async def test_a_server_side_adapter_may_be_used_locally() -> None:
    providers = fake_registry([])
    providers.register(Section.AUDIT, "firehose", lambda c: "server-side adapter")
    config = ServiceConfig.for_testing().with_section(Section.AUDIT, ProviderSelection("firehose"))
    async with ServiceContainer(config, providers) as services:
        assert services.get(Section.AUDIT) == "server-side adapter"


async def test_the_guard_also_covers_model_providers() -> None:
    providers = fake_registry([])
    providers.register(MODEL_PORT, "fake", lambda c: object(), local_only=True, replace=True)
    config = ServiceConfig.for_testing(deployment_env=DeploymentEnv.PROD)
    with pytest.raises(ConfigurationError, match="local development only"):
        await ServiceContainer(config, providers).start()


# ------------------------------------------------------- what a factory sees


async def test_a_factory_sees_only_its_own_section() -> None:
    log: list[str] = []
    config = ServiceConfig.for_testing(
        secrets={"api_key": SecretStr("sk-secret")},
    ).with_section(Section.AUDIT, ProviderSelection("fake", {"path": "audit.jsonl"}))
    async with ServiceContainer(config, fake_registry(log)) as services:
        audit = services.get(Section.AUDIT)
        secrets = services.get(Section.SECRETS)
        assert isinstance(audit, Recorder)
        assert isinstance(secrets, Recorder)
        assert dict(audit.context.selection.options) == {"path": "audit.jsonl"}
        assert audit.context.secret_values == {}
        assert secrets.context.secret_values["api_key"].get_secret_value() == "sk-secret"
        assert not hasattr(audit.context, "config")
        assert "sk-secret" not in repr(secrets.context)


async def test_a_factory_can_use_a_section_built_before_it() -> None:
    providers = fake_registry([])

    def audit_factory(context: BuildContext) -> tuple[str, object]:
        return ("audit using", context.get(Section.SECRETS))

    def secrets_factory(context: BuildContext) -> object:
        return context.get(Section.AUDIT)

    providers.register(Section.AUDIT, "fake", audit_factory, replace=True)
    async with ServiceContainer(ServiceConfig.for_testing(), providers) as services:
        assert services.get(Section.AUDIT) == ("audit using", services.get(Section.SECRETS))

    later = fake_registry([])
    later.register(Section.SECRETS, "fake", secrets_factory, replace=True)
    with pytest.raises(ConfigurationError, match="not built yet"):
        await ServiceContainer(ServiceConfig.for_testing(), later).start()


# ---------------------------------------------------------------- validation


async def test_validate_reports_every_adapter_that_is_not_ready() -> None:
    log: list[str] = []
    providers = fake_registry(
        log, secrets={"invalid": "no key configured"}, audit={"invalid": "path not writable"}
    )
    async with ServiceContainer(ServiceConfig.for_testing(), providers) as services:
        with pytest.raises(ConfigurationError) as caught:
            await services.validate()
    message = str(caught.value)
    assert "secrets (fake): no key configured" in message
    assert "audit (fake): path not writable" in message
    assert "validate checkpoint" in log


async def test_validate_passes_when_every_adapter_is_ready() -> None:
    async with ServiceContainer(ServiceConfig.for_testing(), fake_registry([])) as services:
        await services.validate()


# -------------------------------------------------------------------- models


async def test_each_configured_model_provider_is_built_once_at_startup() -> None:
    log: list[str] = []
    config = ServiceConfig.for_testing(
        model=ModelSection(
            provider="fake",
            model_id="m",
            aliases={"judge": ModelRef(provider="fake", model_id="j")},
        )
    )
    async with ServiceContainer(config, fake_registry(log)) as services:
        assert log.count("build model") == 1
        assert services.model_provider("fake") is services.model_provider("fake")
        ref, provider = services.resolve_model("judge")
        assert ref == ModelRef(provider="fake", model_id="j")
        assert provider is services.model_provider("fake")
        with pytest.raises(ConfigurationError, match="not used by any configured alias"):
            services.model_provider("bedrock")
        with pytest.raises(ConfigurationError, match="'fast' is not configured"):
            services.resolve_model("fast")


async def test_every_model_provider_named_by_an_alias_must_be_registered() -> None:
    config = ServiceConfig.for_testing(
        model=ModelSection(
            provider="fake",
            model_id="m",
            aliases={"judge": ModelRef(provider="elsewhere", model_id="j")},
        )
    )
    with pytest.raises(ConfigurationError, match="'elsewhere' for model is not registered"):
        await ServiceContainer(config, fake_registry([])).start()


# ------------------------------------------------------------------ from_env


async def test_from_env_resolves_configuration_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EAP_MODEL_PROVIDER", "fake")
    for section in Section:
        monkeypatch.setenv(f"EAP_{section.name}_PROVIDER", "fake")
    services = ServiceContainer.from_env(dotenv_path=None, providers=fake_registry([]))
    assert services.config.model.provider == "fake"
    async with services:
        await services.validate()


def test_default_ports_are_the_system_clock_and_random_ids() -> None:
    services = ServiceContainer(ServiceConfig.for_testing(), fake_registry([]))
    assert services.clock.now().tzinfo is not None
    assert services.clock.monotonic() <= services.clock.monotonic()
    assert services.ids.new_id() != services.ids.new_id()


# -------------------------------------------------------------- data sources


def two_sources() -> ServiceConfig:
    return ServiceConfig.for_testing(
        data_sources={
            "accounts": ProviderSelection(provider="fake", options={"table": "a"}),
            "rates": ProviderSelection(provider="fake", options={"table": "r"}),
        }
    )


async def test_each_named_data_source_is_built_with_its_own_name_and_options() -> None:
    log: list[str] = []
    async with ServiceContainer(two_sources(), fake_registry(log)) as services:
        assert services.data_source("accounts") is services.data_source("accounts")
        assert services.data_source("accounts").name == "accounts"
        accounts = services.data_source("accounts")._source
        rates = services.data_source("rates")._source
        assert isinstance(accounts, Recorder)
        assert isinstance(rates, Recorder)
        assert accounts is not rates
        assert (accounts.context.instance, accounts.context.selection.options) == (
            "accounts",
            {"table": "a"},
        )
        assert (rates.context.instance, rates.context.selection.options) == (
            "rates",
            {"table": "r"},
        )
    assert log.count("build data") == 2
    assert log.count("close data") == 2
    # Data sources are built after the sections they may depend on, and closed first.
    assert log.index("build data") > log.index("build secrets")
    assert log.index("close data") < log.index("close secrets")


async def test_an_unknown_data_source_names_the_configured_ones() -> None:
    async with ServiceContainer(two_sources(), fake_registry([])) as services:
        with pytest.raises(ConfigurationError, match="configured data sources: accounts, rates"):
            services.data_source("ledger")


async def test_validation_names_the_data_source_that_is_not_ready() -> None:
    registry = fake_registry([], data={"invalid": "cannot reach the database"})
    async with ServiceContainer(two_sources(), registry) as services:
        with pytest.raises(ConfigurationError) as caught:
            await services.validate()
    assert "data 'accounts' (fake): cannot reach the database" in str(caught.value)
    assert "data 'rates' (fake): cannot reach the database" in str(caught.value)


async def test_an_unregistered_data_source_adapter_stops_startup() -> None:
    config = ServiceConfig.for_testing(
        data_sources={"accounts": ProviderSelection(provider="spreadsheet")}
    )
    with pytest.raises(ConfigurationError, match="'spreadsheet' for data is not registered"):
        await ServiceContainer(config, fake_registry([])).start()
