"""Model providers: the scripted fake, the Anthropic adapter and their wiring."""

from __future__ import annotations

import sys

import anthropic
import httpx2
import pytest
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from pydantic import SecretStr

from ai_agent_lib_core.adapters import (
    AnthropicChatModelProvider,
    FakeChatModel,
    FakeChatModelProvider,
)
from ai_agent_lib_core.contracts import (
    ConfigurationError,
    DeploymentEnv,
    ModelRef,
    ModelSection,
    PolicyDenied,
    ProviderSelection,
    Section,
    ServiceConfig,
    TransientError,
)
from ai_agent_lib_core.di import MODEL_PORT, ServiceContainer, ServiceProviders

# ----------------------------------------------------------------------- fake


async def test_fake_model_replays_its_script_in_order() -> None:
    model = FakeChatModel(responses=["first", AIMessage(content="second")])
    assert (await model.ainvoke([HumanMessage("q1")])).content == "first"
    assert model.invoke([HumanMessage("q2")]).content == "second"


async def test_fake_model_echoes_when_the_script_runs_out() -> None:
    model = FakeChatModel(responses=["scripted"])
    await model.ainvoke([HumanMessage("one")])
    reply = await model.ainvoke([SystemMessage("be brief"), HumanMessage("two")])
    assert reply.content == "fake: two"


async def test_fake_model_records_what_it_was_asked() -> None:
    model = FakeChatModel()
    await model.ainvoke([SystemMessage("s"), HumanMessage("hello there")])
    (call,) = model.calls
    assert [message.content for message in call] == ["s", "hello there"]


async def test_fake_model_can_script_tool_calls_and_errors() -> None:
    tool_call = AIMessage(
        content="", tool_calls=[{"name": "lookup", "args": {"id": 7}, "id": "call-1"}]
    )
    model = FakeChatModel(responses=[tool_call, TransientError("throttled")])
    first = await model.ainvoke([HumanMessage("find 7")])
    assert first.tool_calls[0]["name"] == "lookup"
    with pytest.raises(TransientError, match="throttled"):
        await model.ainvoke([HumanMessage("again")])


async def test_fake_model_reports_token_usage() -> None:
    model = FakeChatModel(responses=["three word answer"])
    reply = await model.ainvoke([HumanMessage("a two")])
    assert reply.usage_metadata == {"input_tokens": 2, "output_tokens": 3, "total_tokens": 5}


def test_fake_model_accepts_bound_tools() -> None:
    model = FakeChatModel()

    def lookup(account: str) -> str:
        """Look an account up."""
        return account

    assert model.bind_tools([lookup]) is model
    assert model.bound_tools == [lookup]


def test_fake_provider_creates_models_and_passes_library_errors_through() -> None:
    provider = FakeChatModelProvider(["hi"])
    model = provider.create("model-x")
    assert model.model_id == "model-x"
    assert provider.models == [model]
    assert provider.capabilities.tool_calling is True
    denied = PolicyDenied("no")
    assert provider.classify_error(denied) is denied
    assert provider.classify_error(ValueError("other")) is None


# ------------------------------------------------------------------ anthropic


def _anthropic_provider() -> AnthropicChatModelProvider:
    return AnthropicChatModelProvider(SecretStr("sk-test-key"), proxy="http://proxy:8080")


def _status_error(status: int) -> anthropic.APIStatusError:
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx2.Response(status, request=request)
    return anthropic.APIStatusError("vendor says: prompt was ...", response=response, body=None)


def test_anthropic_provider_creates_a_chat_model_without_calling_the_network() -> None:
    model = _anthropic_provider().create("claude-model-id")
    assert isinstance(model, ChatAnthropic)
    assert model.model == "claude-model-id"
    assert model.anthropic_api_key.get_secret_value() == "sk-test-key"
    assert model.anthropic_proxy == "http://proxy:8080"
    assert "sk-test-key" not in repr(model)


def test_anthropic_provider_declares_its_capabilities() -> None:
    capabilities = _anthropic_provider().capabilities
    assert capabilities.tool_calling
    assert capabilities.structured_output
    assert capabilities.prompt_caching
    assert capabilities.token_counting
    assert capabilities.context_window is None


@pytest.mark.parametrize("status", [408, 409, 429, 500, 503, 529])
def test_throttling_and_server_errors_map_to_transient_error(status: int) -> None:
    mapped = _anthropic_provider().classify_error(_status_error(status))
    assert isinstance(mapped, TransientError)
    assert str(status) in str(mapped)
    assert "vendor says" not in str(mapped)


def test_connection_problems_map_to_transient_error() -> None:
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    provider = _anthropic_provider()
    assert isinstance(provider.classify_error(anthropic.APITimeoutError(request)), TransientError)
    assert isinstance(
        provider.classify_error(anthropic.APIConnectionError(request=request)), TransientError
    )


@pytest.mark.parametrize("status", [401, 403])
def test_rejected_credentials_map_to_configuration_error(status: int) -> None:
    mapped = _anthropic_provider().classify_error(_status_error(status))
    assert isinstance(mapped, ConfigurationError)
    assert mapped.retryable is False


@pytest.mark.parametrize("error", [_status_error(400), _status_error(404), ValueError("x")])
def test_other_errors_are_left_alone(error: Exception) -> None:
    assert _anthropic_provider().classify_error(error) is None


def test_anthropic_provider_needs_a_key() -> None:
    with pytest.raises(ConfigurationError, match="non-empty API key"):
        AnthropicChatModelProvider(SecretStr(""))


def test_a_missing_optional_dependency_names_the_install_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "langchain_anthropic", None)
    with pytest.raises(ConfigurationError, match=r"pip install 'ai-agent-lib-core\[anthropic\]'"):
        AnthropicChatModelProvider(SecretStr("sk-test-key"))


# --------------------------------------------------------------------- wiring


def _config(model: ModelSection, **overrides: object) -> ServiceConfig:
    return ServiceConfig.for_testing(
        model=model,
        sections={
            Section.SECRETS: ProviderSelection("env"),
        },
        **overrides,
    )


def _registry() -> ServiceProviders:
    providers = ServiceProviders.default()
    for section in Section:
        providers.register(section, "fake", lambda context: object())
    return providers


async def test_an_alias_resolves_to_its_provider_and_model() -> None:
    model = ModelSection(
        provider="fake",
        model_id="fake-default",
        aliases={"judge": ModelRef(provider="anthropic", model_id="claude-judge")},
    )
    config = _config(model, secrets={"anthropic_api_key": SecretStr("sk-test-key")})
    async with ServiceContainer(config, _registry()) as services:
        ref, provider = services.resolve_model("default")
        assert ref == ModelRef(provider="fake", model_id="fake-default")
        assert isinstance(provider, FakeChatModelProvider)

        ref, provider = services.resolve_model("judge")
        assert ref == ModelRef(provider="anthropic", model_id="claude-judge")
        assert isinstance(provider, AnthropicChatModelProvider)


async def test_the_anthropic_provider_fails_at_startup_without_its_secret() -> None:
    config = _config(ModelSection(provider="anthropic", model_id="claude-model-id"))
    with pytest.raises(ConfigurationError, match="anthropic_api_key"):
        await ServiceContainer(config, _registry()).start()


async def test_the_fake_model_provider_is_for_local_development_only() -> None:
    assert ServiceProviders.default().lookup(MODEL_PORT, "fake").local_only is True
    assert ServiceProviders.default().lookup(MODEL_PORT, "anthropic").local_only is False
    config = _config(ModelSection(provider="fake", model_id="m"), deployment_env=DeploymentEnv.PROD)
    with pytest.raises(ConfigurationError, match="local development only"):
        await ServiceContainer(config, _registry()).start()


async def test_a_service_that_configures_no_model_builds_no_model_provider() -> None:
    """An MCP server needs no model, so it must not need a model's credentials."""
    config = _config(ModelSection(provider="anthropic"))
    async with ServiceContainer(config, _registry()) as services:
        with pytest.raises(ConfigurationError, match="not used by any configured alias"):
            services.model_provider("anthropic")
        with pytest.raises(ConfigurationError, match="model alias 'default' is not configured"):
            services.model()
