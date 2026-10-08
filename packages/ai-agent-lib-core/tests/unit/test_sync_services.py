"""The synchronous front: a script or a WSGI app runs the agent without async."""

from __future__ import annotations

from typing import Annotated, Any, TypedDict

import pytest
from langchain_core.messages import AnyMessage, HumanMessage
from langgraph.graph import START, StateGraph
from langgraph.graph.message import add_messages

from ai_agent_lib_core import Principal, RequestContext, ServiceConfig, SyncServices
from ai_agent_lib_core.contracts import ProviderSelection, Section
from ai_agent_lib_core.testing import FakeChatModelProvider, FakeIdentityVerifier, Fakes


class State(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]


def build_graph(services: Any) -> Any:
    model = services.model()

    async def agent(state: State) -> dict[str, list[AnyMessage]]:
        return {"messages": [await model.ainvoke(state["messages"])]}

    builder = StateGraph(State, context_schema=RequestContext)
    builder.add_node("agent", agent)
    builder.add_edge(START, "agent")
    return builder.compile(**services.compile_kwargs())


def test_a_graph_runs_from_plain_synchronous_code() -> None:
    fakes = Fakes(
        model=FakeChatModelProvider(["Hello, Ada."]),
        identity=FakeIdentityVerifier({"u-1": Principal(subject="ann", tenant="t-1")}),
    )
    with SyncServices(fakes.config(), fakes.providers()) as services:
        graph = build_graph(services.container)
        context = services.authenticate("u-1", application="accounts-agent", thread_id="t-1")
        result = services.invoke(graph, {"messages": [HumanMessage("Say hello")]}, context)
        services.validate()
    assert result["messages"][-1].content == "Hello, Ada."
    assert [record.event for record in fakes.audit.records] == ["model.call"]


def test_the_services_are_closed_when_the_block_ends() -> None:
    fakes = Fakes()
    services = SyncServices(fakes.config(), fakes.providers())
    with services:
        pass
    with pytest.raises(RuntimeError, match="not started"):
        _ = services.container


def test_a_container_that_cannot_start_raises_and_leaves_no_thread_behind() -> None:
    config = ServiceConfig.for_testing(sections={Section.AUDIT: ProviderSelection("missing")})
    services = SyncServices(config, Fakes().providers())
    with pytest.raises(Exception, match="missing"), services:
        pass
    assert not services._thread.is_alive()


def test_a_container_that_cannot_be_built_leaves_no_thread_behind() -> None:
    services = SyncServices(Fakes().config(), Fakes().providers(), no_such_option=1)
    with pytest.raises(TypeError), services:
        pass
    assert not services._thread.is_alive()
    assert services._loop.is_closed()
