"""Regressions for the findings of the external review of commit 2215b37.

Each test names its finding (R1, R2, ...) and fails on the code as it was.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from langchain_core.caches import InMemoryCache
from langchain_core.globals import set_llm_cache
from langchain_core.messages import HumanMessage
from pydantic import ValidationError

from ai_agent_lib_core.adapters import FakeChatModelProvider
from ai_agent_lib_core.contracts import (
    AgentEntry,
    AuditOutcome,
    Classification,
    ConfigurationError,
    GuardrailPoint,
    PolicyDenied,
    Principal,
    RequestContext,
    ServerEntry,
    ToolEntry,
    ValidationFailed,
)
from ai_agent_lib_core.evaluation import CaseResult, EvalCase, EvalReport, Score
from ai_agent_lib_core.pipeline import (
    AgentCheck,
    RegistryInterceptor,
    ToolCall,
    bind_request_context,
)
from ai_agent_lib_core.testing import FakeGuardrails, FakeRegistry, Fakes

CALLER = RequestContext(
    principal=Principal(subject="u-1", tenant="t-1"),
    application="accounts-agent",
    request_id="r-1",
    thread_id="th-1",
)


@pytest.fixture
def global_llm_cache() -> Iterator[None]:
    set_llm_cache(InMemoryCache())
    yield
    set_llm_cache(None)


@pytest.mark.usefixtures("global_llm_cache")
async def test_r1_a_global_llm_cache_never_answers_for_the_governed_model() -> None:
    fakes = Fakes(model=FakeChatModelProvider(["first", "second"]))
    async with fakes.container() as services:
        model = services.model()
        with bind_request_context(CALLER):
            await model.ainvoke([HumanMessage("the same question")])
            again = await model.ainvoke([HumanMessage("the same question")])

    # Both calls went through the pipeline and reached the provider: no cached answer.
    assert [record.event for record in fakes.audit.records] == ["model.call", "model.call"]
    assert again.content == "second"
    assert len(fakes.model.models[0].calls) == 2


async def test_r2_the_agents_ceiling_travels_with_the_call_into_the_tool() -> None:
    server = ServerEntry(
        id="accounts",
        owner="treasury",
        url="http://localhost:8001/mcp",
        audience="accounts-mcp",
        tools=(ToolEntry(name="accounts.lookup", version="1", read_only=True),),
    )
    agent = AgentEntry(
        id="accounts-agent",
        owner="treasury",
        version="1",
        mcp_servers=("accounts",),
        classification_ceiling=Classification.INTERNAL,
    )
    registry = FakeRegistry(agents=[agent], servers=[server])
    seen: list[RequestContext | None] = []

    async def tool(call: ToolCall) -> str:
        seen.append(call.context)
        return "ran"

    caller = RequestContext(
        principal=Principal(subject="u-1", tenant="t-1"),
        application="accounts-agent",
        request_id="r-1",
        thread_id="th-1",
        classification_ceiling=Classification.RESTRICTED,
    )
    interceptor: RegistryInterceptor[str] = RegistryInterceptor(
        registry, agent_check=AgentCheck.SELF
    )
    await interceptor(ToolCall(context=caller, tool="accounts.lookup", server="accounts"), tool)

    # Whatever the tool does next (a query, a nested call) runs under the lower of the two.
    (context,) = seen
    assert context is not None
    assert context.classification_ceiling is Classification.INTERNAL


async def test_r6_two_tools_with_one_name_cannot_be_bound_to_the_model() -> None:
    def search(query: str) -> str:
        """Search one place."""
        return query

    def search_again(query: str) -> str:
        """Search another place."""
        return query

    search_again.__name__ = "search"
    async with Fakes().container() as services:
        tools = services.tools([search]) + services.tools([search_again])
        with pytest.raises(ConfigurationError) as caught:
            services.model().bind_tools(tools)
    assert "two tools offered to the model have the name 'search'" in str(caught.value)


def greet(name: str) -> str:
    """Greet someone by name."""
    return f"Hello, {name}!"


async def test_r8_a_single_plain_input_works_as_it_does_natively() -> None:
    async with Fakes().container() as services:
        (tool,) = services.tools([greet], read_only=["greet"])
        with bind_request_context(CALLER):
            # The result is framed as untrusted data, as every tool result is.
            assert "Hello, Ada!" in await tool.ainvoke("Ada")


async def test_r9_a_malformed_call_still_goes_through_policy_and_audit() -> None:
    fakes = Fakes()
    async with fakes.container() as services:
        (tool,) = services.tools([greet], read_only=["greet"])
        with bind_request_context(CALLER), pytest.raises(ValidationError):
            await tool.ainvoke({"nom": "Ada"})

    assert len(fakes.policy.requests) == 1
    (record,) = fakes.audit.records
    assert record.event == "tool.call"
    assert record.outcome is not AuditOutcome.SUCCESS


def test_r15_a_score_that_is_not_a_number_is_refused() -> None:
    with pytest.raises(ValueError, match="a score is a number from 0 to 1"):
        Score("quality", float("nan"))
    with pytest.raises(ValueError, match="a score is a number from 0 to 1"):
        Score("quality", 1.5)


def test_r15_a_metric_with_no_scores_fails_its_bar_and_a_bar_must_be_a_number() -> None:
    case = EvalCase(id="c-1", input={}, expected={})
    report = EvalReport((CaseResult(case, (), None, 0.0),), ("quality",))
    assert report.means == {"quality": 0.0}
    with pytest.raises(ValidationFailed):
        report.require({"quality": 0.99})
    with pytest.raises(ValueError, match="a bar is a number from 0 to 1"):
        report.require({"quality": float("nan")})


async def test_r16_tokens_of_a_reply_a_guardrail_blocks_are_still_charged() -> None:
    def block_answers(point: GuardrailPoint, text: str) -> str | None:
        return "pii" if point is GuardrailPoint.MODEL_OUTPUT else None

    fakes = Fakes(
        model=FakeChatModelProvider(["one reply", "another reply"]),
        guardrails=FakeGuardrails(block_answers),
    )
    async with fakes.container() as services:
        model = services.model()
        with bind_request_context(CALLER):
            for _ in range(2):
                with pytest.raises(PolicyDenied):
                    await model.ainvoke([HumanMessage("a question")])

    blocked = [record.attributes for record in fakes.audit.records]
    # The provider answered both times (2 tokens in, 2 out), so both count.
    assert [(a["input_tokens"], a["output_tokens"]) for a in blocked] == [(2, 2), (2, 2)]
    assert [a["budget_tokens"] for a in blocked] == [4, 8]
