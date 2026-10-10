"""The container builds once, guards, validates and closes in reverse order."""

from __future__ import annotations

import asyncio
from typing import Any

import anyio
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
    ServicePort,
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


async def test_declared_dependencies_reorder_construction_and_teardown() -> None:
    log: list[str] = []
    providers = fake_registry(log)

    def secrets(context: BuildContext) -> Recorder:
        assert isinstance(context.get("guardrails"), Recorder)
        return Recorder(context, log)

    providers.register("secrets", "fake", secrets, dependencies=("guardrails",), replace=True)
    async with ServiceContainer(ServiceConfig.for_testing(), providers):
        assert log[:2] == ["build guardrails", "build secrets"]
    assert log[-2:] == ["close secrets", "close guardrails"]


@pytest.mark.parametrize("dependency", ["missing", "secrets"])
async def test_bad_dependency_graph_fails_before_any_factory(dependency: str) -> None:
    log: list[str] = []
    providers = fake_registry(log)
    providers.register(
        "secrets",
        "fake",
        lambda context: Recorder(context, log),
        dependencies=(dependency,),
        replace=True,
    )
    with pytest.raises(ConfigurationError):
        await ServiceContainer(ServiceConfig.for_testing(), providers).start()
    assert log == []


async def test_undeclared_dependency_fails_and_closes_built_resources() -> None:
    log: list[str] = []
    providers = fake_registry(log)
    providers.register(
        "audit", "fake", lambda context: context.get("secrets"), dependencies=(), replace=True
    )
    with pytest.raises(ConfigurationError, match="undeclared dependency"):
        await ServiceContainer(ServiceConfig.for_testing(), providers).start()
    assert log == ["build secrets", "close secrets"]


async def test_typed_port_rejects_incompatible_adapter_and_still_closes_it() -> None:
    log: list[str] = []
    providers = fake_registry(log)
    port = ServicePort[Recorder]("audit", ("write",))
    providers.register(port, "fake", lambda context: Recorder(context, log), replace=True)
    with pytest.raises(ConfigurationError, match="missing required members: write"):
        await ServiceContainer(ServiceConfig.for_testing(), providers).start()
    assert log[-2:] == ["close audit", "close secrets"]


async def test_construction_and_teardown_share_one_owner_task() -> None:
    log: list[str] = []
    providers = fake_registry(log)
    task: asyncio.Task[Any] | None = None

    class AffineAdapter:
        async def aclose(self) -> None:
            assert asyncio.current_task() is task

    def build(context: BuildContext) -> AffineAdapter:
        nonlocal task
        task = asyncio.current_task()
        return AffineAdapter()

    providers.register("audit", "fake", build, replace=True)
    async with ServiceContainer(ServiceConfig.for_testing(), providers):
        assert task is not None


async def test_background_failure_does_not_cancel_other_adapters_cleanup() -> None:
    trigger = asyncio.Event()
    log: list[str] = []
    providers = fake_registry(log)

    class YieldingRecorder(Recorder):
        async def aclose(self) -> None:
            await asyncio.sleep(0)
            await super().aclose()

    for port in [*(section.value for section in Section), MODEL_PORT]:
        providers.register(
            port, "fake", lambda context: YieldingRecorder(context, log), replace=True
        )

    async def child() -> None:
        await trigger.wait()
        raise ValueError("background failure")

    async def build(context: BuildContext) -> object:
        group = anyio.create_task_group()
        await group.__aenter__()
        group.start_soon(child)

        class AffineAdapter:
            async def aclose(self) -> None:
                log.append("close audit")
                await group.__aexit__(None, None, None)

        return AffineAdapter()

    providers.register("audit", "fake", build, replace=True)
    services = ServiceContainer(ServiceConfig.for_testing(), providers)
    await services.start()
    log.clear()
    trigger.set()
    async with asyncio.timeout(2):
        while not services._closed:
            await asyncio.sleep(0)
    with pytest.raises(ExceptionGroup) as caught:
        await services.aclose()
    assert "background failure" in repr(caught.value)
    assert sorted(log) == sorted(f"close {port}" for port in [*Section, MODEL_PORT])
    await services.aclose()  # failure is reported by the first public close


async def test_an_adapter_shared_by_two_factories_is_closed_once() -> None:
    log: list[str] = []
    providers = fake_registry(log)
    providers.register("audit", "fake", lambda context: context.get("secrets"), replace=True)
    async with ServiceContainer(ServiceConfig.for_testing(), providers):
        pass
    assert log.count("close secrets") == 1


@pytest.mark.parametrize("immediate", [False, True])
async def test_background_failure_during_startup_interrupts_a_pending_factory(
    immediate: bool,
) -> None:
    trigger, entered, released = asyncio.Event(), asyncio.Event(), asyncio.Event()
    providers = fake_registry([])

    async def child() -> None:
        if not immediate:
            await trigger.wait()
        raise ValueError("failed during startup")

    async def audit(context: BuildContext) -> object:
        group = anyio.create_task_group()
        await group.__aenter__()
        group.start_soon(child)

        class Adapter:
            async def aclose(self) -> None:
                await group.__aexit__(None, None, None)

        return Adapter()

    async def identity(context: BuildContext) -> object:
        entered.set()
        if immediate:

            class ReadyAdapter:
                async def aclose(self) -> None:
                    await asyncio.sleep(0)
                    released.set()

            return ReadyAdapter()
        try:
            await asyncio.Event().wait()
        finally:
            released.set()
        return object()

    providers.register("audit", "fake", audit, replace=True)
    providers.register("identity", "fake", identity, replace=True)
    services = ServiceContainer(ServiceConfig.for_testing(), providers)
    starting = asyncio.create_task(services.start())
    async with asyncio.timeout(2):
        if not immediate:
            await entered.wait()
            trigger.set()
        with pytest.raises(ExceptionGroup) as caught:
            await starting
    assert "failed during startup" in repr(caught.value)
    assert released.is_set() == entered.is_set()
    await services.aclose()


async def test_secondary_background_failure_cannot_interrupt_startup_cleanup() -> None:
    trigger, failed, release, closed = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )
    providers = fake_registry([])

    async def child() -> None:
        await trigger.wait()
        raise ValueError("secondary failure")

    async def secrets(context: BuildContext) -> object:
        group = anyio.create_task_group()
        await group.__aenter__()
        group.start_soon(child)

        class Adapter:
            async def aclose(self) -> None:
                try:
                    await group.__aexit__(None, None, None)
                finally:
                    failed.set()

        return Adapter()

    class SlowAudit:
        async def aclose(self) -> None:
            trigger.set()
            await release.wait()
            closed.set()

    def identity(context: BuildContext) -> object:
        raise ValueError("startup failure")

    providers.register("secrets", "fake", secrets, replace=True)
    providers.register("audit", "fake", lambda _: SlowAudit(), replace=True)
    providers.register("identity", "fake", identity, replace=True)
    services = ServiceContainer(ServiceConfig.for_testing(), providers)
    starting = asyncio.create_task(services.start())
    async with asyncio.timeout(2):
        await failed.wait()
        await asyncio.sleep(0)
        try:
            assert not starting.done()
            assert not services._closed
        finally:
            release.set()
        with pytest.raises(ExceptionGroup) as caught:
            await starting
    assert "startup failure" in repr(caught.value)
    assert "secondary failure" in repr(caught.value)
    assert closed.is_set()


async def test_cancellation_during_construction_closes_earlier_adapters() -> None:
    log: list[str] = []
    entered = asyncio.Event()
    providers = fake_registry(log)

    async def build(context: BuildContext) -> object:
        entered.set()
        await asyncio.Event().wait()
        return object()

    providers.register("audit", "fake", build, replace=True)
    services = ServiceContainer(ServiceConfig.for_testing(), providers)
    starting = asyncio.create_task(services.start())
    await entered.wait()
    starting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await starting
    assert log == ["build secrets", "close secrets"]


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


async def test_a_cancelled_closer_does_not_skip_other_adapters() -> None:
    log: list[str] = []
    providers = fake_registry(log)

    class CancelledAdapter:
        async def aclose(self) -> None:
            log.append("cancelled guardrails")
            raise asyncio.CancelledError

    providers.register(Section.GUARDRAILS, "fake", lambda _: CancelledAdapter(), replace=True)
    services = ServiceContainer(ServiceConfig.for_testing(), providers)
    await services.start()
    with pytest.raises(asyncio.CancelledError):
        await services.aclose()
    await services.aclose()
    assert log[-1] == "close secrets"
    assert log.count("close audit") == 1


async def test_cancelling_a_close_caller_waits_for_shared_cleanup() -> None:
    log: list[str] = []
    entered, release = asyncio.Event(), asyncio.Event()
    providers = fake_registry(log)

    class SlowAdapter:
        async def aclose(self) -> None:
            entered.set()
            await release.wait()
            log.append("close slow adapter")

    providers.register(Section.GUARDRAILS, "fake", lambda _: SlowAdapter(), replace=True)
    services = ServiceContainer(ServiceConfig.for_testing(), providers)
    await services.start()
    first = asyncio.create_task(services.aclose())
    await entered.wait()
    second = asyncio.create_task(services.aclose())
    first.cancel()
    await asyncio.sleep(0)
    assert not first.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await first
    await second
    assert log[-1] == "close secrets"
    assert log.count("close slow adapter") == 1


async def test_startup_preserves_the_original_error_when_cleanup_also_fails() -> None:
    providers = fake_registry([], audit={"close_error": "close failed"})

    def broken(_: BuildContext) -> object:
        raise ConfigurationError("startup failed")

    providers.register(Section.IDENTITY, "fake", broken, replace=True)
    with pytest.raises(BaseExceptionGroup) as caught:
        await ServiceContainer(ServiceConfig.for_testing(), providers).start()
    assert isinstance(caught.value.exceptions[0], ConfigurationError)
    assert "startup failed" in str(caught.value.exceptions[0])


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
