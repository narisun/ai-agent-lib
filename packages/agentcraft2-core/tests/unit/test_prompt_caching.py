"""Cache settings reach the Anthropic wire payload without changing saved messages."""

import json
from contextlib import nullcontext
from pathlib import Path
from typing import Any, Literal

import pytest
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from pydantic import SecretStr

from agentcraft2_core.adapters import AnthropicChatModelProvider, AnthropicModelOptions
from agentcraft2_core.config import ConfigResolver, MappingConfigSource
from agentcraft2_core.contracts import (
    ConfigurationError,
    ModelRef,
    ModelSection,
    Principal,
    ProviderSelection,
    RequestContext,
    ServiceConfig,
)
from agentcraft2_core.di import ServiceContainer
from agentcraft2_core.integrations.langgraph import static_system_prompt
from agentcraft2_core.observability.live import LiveRun, bind_live_run
from agentcraft2_core.pipeline import bind_request_context
from agentcraft2_core.testing import FakeChatModelProvider, Fakes


def provider(enabled: bool = True, ttl: Literal["5m", "1h"] = "5m") -> AnthropicChatModelProvider:
    return AnthropicChatModelProvider(
        SecretStr("sk-test-key"),
        options=AnthropicModelOptions(prompt_caching=enabled, cache_ttl=ttl),
    )


@pytest.mark.parametrize("ttl", ["5m", "1h"])
def test_static_breakpoint_and_automatic_cache_reach_anthropic_payload(
    ttl: Literal["5m", "1h"],
) -> None:
    adapter = provider(ttl=ttl)
    messages = [static_system_prompt("Fixed instructions", "Client and date"), HumanMessage("q")]
    before = [m.model_dump() for m in messages]
    prepared, options = adapter.prepare_request(messages, {})
    native = adapter.create("claude-sonnet-4-5")
    assert isinstance(native, ChatAnthropic)
    payload = native._get_request_payload(prepared, **options)
    control = {"type": "ephemeral", "ttl": ttl}
    assert payload["cache_control"] == control
    assert payload["system"][0] == {
        "type": "text",
        "text": "Fixed instructions",
        "cache_control": control,
    }
    assert payload["system"][1] == {"type": "text", "text": "Client and date"}
    assert [m.model_dump() for m in messages] == before
    # Native calls also get automatic caching; the governed integration adds the breakpoint.
    assert native._get_request_payload([HumanMessage("q")])["cache_control"] == control


def test_caching_off_preserves_the_request_and_adds_no_directives() -> None:
    adapter = provider(enabled=False)
    messages = [static_system_prompt("Fixed", "Changing"), HumanMessage("q")]
    prepared, options = adapter.prepare_request(messages, {})
    native = adapter.create("claude-sonnet-4-5")
    assert isinstance(native, ChatAnthropic)
    payload = native._get_request_payload(prepared, **options)
    assert prepared == messages
    assert "cache_control" not in json.dumps(payload)


def test_plain_system_messages_use_automatic_cache_without_inventing_a_static_boundary() -> None:
    messages = [SystemMessage("Possibly dynamic"), HumanMessage("q")]
    prepared, options = provider().prepare_request(messages, {})
    assert prepared == messages
    assert options == {"cache_control": {"type": "ephemeral", "ttl": "5m"}}


@pytest.mark.parametrize("raw", ["[]", '{"anthropic":true}', '{"anthropic":null}'])
def test_invalid_model_options_shape_is_rejected(raw: str) -> None:
    with pytest.raises(ConfigurationError):
        ConfigResolver(MappingConfigSource({"AC2_MODEL_OPTIONS": raw})).resolve()


@pytest.mark.parametrize(
    "options", [{"prompt_cache": True}, {"cache_ttl": "10m"}, {"prompt_caching": "yes"}]
)
def test_invalid_cache_settings_are_rejected(options: dict[str, object]) -> None:
    with pytest.raises(ConfigurationError):
        ProviderSelection("anthropic", options).parse_options(AnthropicModelOptions)


def test_provider_options_are_immutable_and_shared_by_aliases() -> None:
    raw = {"AC2_MODEL_OPTIONS": '{"anthropic":{"prompt_caching":true}}'}
    resolved = ConfigResolver(MappingConfigSource(raw)).resolve()
    model = ModelSection(
        "fake",
        "fake-model",
        {"writer": ModelRef("anthropic", "claude-sonnet-4-5")},
        options=resolved.model.options,
    )
    ref = model.resolve("writer")
    assert model.selection(ref.provider).parse_options(AnthropicModelOptions).prompt_caching
    assert not model.selection("fake").options
    with pytest.raises(TypeError):
        model.options["anthropic"]["prompt_caching"] = False  # type: ignore[index]


@pytest.mark.parametrize("streaming", [False, True])
async def test_request_preparation_is_traced_and_each_call_remains_governed(
    tmp_path: Path, streaming: bool
) -> None:
    adapter = provider()

    class PreparedFake(FakeChatModelProvider):
        def prepare_request(
            self, messages: list[BaseMessage], options: dict[str, Any]
        ) -> tuple[list[BaseMessage], dict[str, Any]]:
            return adapter.prepare_request(messages, options)

    path = tmp_path / "trace.jsonl"
    fakes = Fakes(model=PreparedFake(["one", "two"]))
    context = RequestContext(
        principal=Principal(subject="dev", tenant="test"),
        application="helper",
        request_id="cache-test",
        thread_id="same-thread",
    )

    async def sink(call: int, text: str, *, reset: bool = False) -> None:
        pass

    live = LiveRun(context, sink)
    async with fakes.container(ServiceConfig.for_testing(model_trace_path=path)) as services:
        model = services.model().bind_tools([])
        with bind_request_context(context), bind_live_run(live) if streaming else nullcontext():
            for question in ("one", "two"):
                await model.ainvoke(
                    [static_system_prompt("Fixed", question), HumanMessage(question)]
                )
        assert len(fakes.audit.records) == 2
    records = [json.loads(line) for line in path.read_text().splitlines()]
    requests = [record for record in records if record["event"] == "model.request"]
    assert len(requests) == 2
    for request, call in zip(requests, fakes.model.models[0].calls, strict=True):
        assert request["invocation_options"]["cache_control"]["type"] == "ephemeral"
        assert request["messages"] == [m.model_dump() for m in call]
        assert request["messages"][0]["content"][0]["cache_control"]["ttl"] == "5m"
    assert requests[0]["messages"][0]["content"][0] == requests[1]["messages"][0]["content"][0]


async def test_unused_provider_options_fail_startup_if_misspelled() -> None:
    config = ServiceConfig.for_testing(
        model=ModelSection("fake", "fake-model", options={"anthropic": {"prompt_cache": True}})
    )
    providers = Fakes().providers()
    providers.register("model", "anthropic", lambda context: None, options=AnthropicModelOptions)
    with pytest.raises(ConfigurationError, match="prompt_cache"):
        async with ServiceContainer(config, providers):
            pass
