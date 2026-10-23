"""Inspect the generated helper agent's tool loop and checkpointed conversation.

Run from the tutorial workspace with helper installed. Scripted model replies
make the sequence repeatable; LangGraph executes the real graph and tool.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

from helper.graph import APPLICATION, answer, build_graph

from ai_agent_lib_core import Principal, RequestContext, ServiceConfig
from ai_agent_lib_core.pipeline import bind_request_context
from ai_agent_lib_core.testing import FakeChatModelProvider, Fakes, calls_tool


async def main() -> None:
    """Run two turns on one in-memory checkpoint thread and write their trace."""
    trace_path = Path(".agentcraft2/react-demo.jsonl")
    config = ServiceConfig.for_testing(model_trace_path=trace_path)
    fakes = Fakes(
        model=FakeChatModelProvider(
            [calls_tool("greet", name="Ada"), "Hello, Ada!", "Your name is Ada."]
        )
    )
    context = RequestContext(
        principal=Principal(subject="learner", tenant="tutorial"),
        application=APPLICATION,
        request_id="demo-turn-1",
        thread_id="react-demo",
    )
    async with fakes.container(config) as services:
        graph = build_graph(services)
        first = await answer(graph, services, context, "My name is Ada. Please greet me.")
        second = await answer(
            graph,
            services,
            replace(context, request_id="demo-turn-2"),
            "What is my name?",
        )
        # Checkpoint inspection also needs the owning caller's scope.
        with bind_request_context(context):
            snapshot = await graph.aget_state(services.invocation(context)["config"])
        stored_types = [message.type for message in snapshot.values["messages"]]

    counts = [len(messages) for messages in fakes.model.models[0].calls]
    print(first)  # noqa: T201 - the tutorial's expected output
    print(second)  # noqa: T201 - the tutorial's expected output
    print(f"Messages sent on each model call: {counts}")  # noqa: T201
    print(f"Checkpoint message types: {stored_types}")  # noqa: T201
    print(f"Trace: {trace_path.resolve()}")  # noqa: T201


if __name__ == "__main__":
    asyncio.run(main())
