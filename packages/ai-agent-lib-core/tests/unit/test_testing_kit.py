"""The testing kit and the public API surface."""

from __future__ import annotations

import importlib
import subprocess
import sys

import pytest
from langchain_core.messages import HumanMessage

import ai_agent_lib_core
from ai_agent_lib_core import Principal, RequestContext, ServiceConfig, ServiceContainer
from ai_agent_lib_core.pipeline import bind_request_context
from ai_agent_lib_core.testing import (
    FAKE_PROVIDER,
    FakeChatModelProvider,
    Fakes,
    FrozenClock,
    SequentialIds,
    fake_providers,
)

PACKAGES = [
    "ai_agent_lib_core",
    "ai_agent_lib_core.adapters",
    "ai_agent_lib_core.config",
    "ai_agent_lib_core.contracts",
    "ai_agent_lib_core.di",
    "ai_agent_lib_core.integrations.langgraph",
    "ai_agent_lib_core.pipeline",
    "ai_agent_lib_core.testing",
    "ai_agent_lib_core.testing.contracts",
    "ai_agent_lib_core.testing.langgraph_contracts",
]

CONTEXT = RequestContext(
    principal=Principal(subject="u-1", tenant="t-9"),
    application="app",
    request_id="r-1",
    thread_id="th-1",
)


@pytest.mark.parametrize("name", PACKAGES)
def test_every_public_name_is_declared_and_importable(name: str) -> None:
    module = importlib.import_module(name)
    exported = module.__all__
    assert exported, f"{name} declares no public names"
    assert len(exported) == len(set(exported))
    for item in exported:
        assert hasattr(module, item), f"{name}.{item} is exported but missing"


def test_the_top_level_package_offers_the_names_applications_need() -> None:
    for name in ("RequestContext", "ServiceContainer", "Principal", "PolicyDenied"):
        assert name in ai_agent_lib_core.__all__


def test_importing_the_package_does_not_load_langgraph() -> None:
    code = "import sys, ai_agent_lib_core; sys.exit(int('langgraph' in sys.modules))"
    result = subprocess.run(  # noqa: S603 - fixed command, no untrusted input
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, "importing ai_agent_lib_core must not import langgraph"


@pytest.mark.parametrize(
    "module",
    ["ai_agent_lib_core", "ai_agent_lib_core.testing", "ai_agent_lib_core.integrations.langgraph"],
)
def test_the_mcp_sdk_is_loaded_only_by_code_that_uses_mcp(module: str) -> None:
    code = f"import sys, {module}; sys.exit(int('mcp' in sys.modules))"
    result = subprocess.run(  # noqa: S603 - fixed command, no untrusted input
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, f"importing {module} must not import the MCP SDK"


def test_an_mcp_server_does_not_load_langgraph() -> None:
    code = (
        "import sys, ai_agent_lib_core.integrations.mcp; sys.exit(int('langgraph' in sys.modules))"
    )
    result = subprocess.run(  # noqa: S603 - fixed command, no untrusted input
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, "the server side of the MCP bindings must not import langgraph"


async def test_fakes_wire_a_container_whose_fakes_can_be_inspected() -> None:
    fakes = Fakes(model=FakeChatModelProvider(["hello"]))
    async with fakes.container() as services:
        await services.validate()
        assert services.clock is fakes.clock
        assert services.ids is fakes.ids
        assert services.telemetry is fakes.telemetry
        with bind_request_context(CONTEXT):
            await services.model().ainvoke([HumanMessage("hi")])
    assert fakes.audit.records[0].record_id == "id-1"
    assert fakes.telemetry.events[0][0] == "model.call"


async def test_fake_providers_accepts_a_script_or_a_provider() -> None:
    for model in (["scripted"], FakeChatModelProvider(["scripted"])):
        services = ServiceContainer(ServiceConfig.for_testing(), fake_providers(model=model))
        async with services:
            with bind_request_context(CONTEXT):
                reply = await services.model().ainvoke([HumanMessage("hi")])
        assert reply.content == "scripted"
    assert fake_providers().names("audit") == (FAKE_PROVIDER,)


def test_frozen_clock_moves_only_when_told() -> None:
    clock = FrozenClock()
    start, tick = clock.now(), clock.monotonic()
    assert (clock.now(), clock.monotonic()) == (start, tick)
    clock.advance(1.5)
    assert (clock.now() - start).total_seconds() == 1.5
    assert clock.monotonic() == tick + 1.5
    with pytest.raises(ValueError, match="backwards"):
        clock.advance(-1)


def test_sequential_ids_are_predictable() -> None:
    ids = SequentialIds("rec")
    assert [ids.new_id(), ids.new_id()] == ["rec-1", "rec-2"]


async def test_fake_data_sources_are_served_by_name_and_record_their_calls() -> None:
    from ai_agent_lib_core.contracts import (
        Classification,
        ConfigurationError,
        Obligations,
        ProviderSelection,
    )
    from ai_agent_lib_core.testing import (
        FakeDataSource,
        FakePolicyDecisionPoint,
        FakeQuery,
        fake_accounts_source,
    )

    caller = RequestContext(
        principal=Principal(subject="u-1", tenant="t-1"),
        application="accounts-mcp",
        request_id="r-1",
        thread_id="th-1",
        classification_ceiling=Classification.RESTRICTED,
    )
    obligations = Obligations(max_rows=1)
    rates = FakeDataSource(
        "rates", [FakeQuery.static("latest", ["pair", "rate"], [("EURUSD", 1.1)])]
    )
    fakes = Fakes(
        data_sources={"accounts": fake_accounts_source(), "rates": rates},
        policy=FakePolicyDecisionPoint(
            lambda request: obligations if request.resource.name.startswith("accounts.") else True
        ),
    )
    async with fakes.container() as services:
        result = await services.data_source("rates").query("latest", context=caller)
        assert result.as_dicts() == [{"pair": "EURUSD", "rate": 1.1}]
        with bind_request_context(caller):
            capped = await services.data_source("accounts").query(
                "accounts_by_region", {"region": "east"}
            )
        assert len(capped.rows) == 1
    assert rates.calls == [("latest", {}, Obligations())]
    accounts = fakes.data_sources["accounts"]
    assert isinstance(accounts, FakeDataSource)
    assert accounts.calls[0][0] == "accounts_by_region"
    assert accounts.calls[0][2] == obligations
    assert [record.event for record in fakes.audit.records] == ["data.query", "data.query"]

    missing = ServiceConfig.for_testing(data_sources={"ledger": ProviderSelection(provider="fake")})
    with pytest.raises(ConfigurationError, match="no fake data source called 'ledger'"):
        await Fakes().container(missing).start()
