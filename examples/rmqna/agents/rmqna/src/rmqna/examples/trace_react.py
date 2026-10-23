"""Inspect rmqna's tool loop and checkpointed conversation against the real rmqna-data server.

Run from the workspace with rmqna and rmqna-data installed. The model's replies are scripted so the
sequence repeats; the server, its SQL engine, the rules, the audit log and the SQLite checkpoint
are the real ones, in this process. Nothing needs to be running and nothing is sent anywhere.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Any

from ai_agent_lib_core import ServiceContainer
from ai_agent_lib_core.pipeline import bind_request_context
from ai_agent_lib_core.testing import calls_tool, load_test_config, scripted_providers
from ai_agent_lib_core.testing.mcp import InProcessMcpConnector

from rmqna.graph import APPLICATION, answer, build_graph
from rmqna.settings import ENV_FILE
from rmqna_data import SERVER_ID, build_server

WORKSPACE = ENV_FILE.parent.parents[1]
QUESTIONS = (
    "Which Canadian clients are there?",
    "Which of them has the largest facility commitment?",
)
FIRST_SQL = "SELECT uen, name FROM accounts WHERE country = 'Canada' ORDER BY name"
SECOND_SQL = (
    "SELECT a.name, MAX(f.commitment) AS largest_commitment "
    "FROM facilities f JOIN accounts a ON a.uen = f.uen "
    "WHERE a.country = 'Canada' GROUP BY a.name ORDER BY largest_commitment DESC LIMIT 1"
)
ANSWERS = (
    "Nine Canadian clients, from Boston Pizza International Inc. to St-Hubert Group Inc.\n\n"
    "Basis: accounts, country = 'Canada'. As of the 2026-06-30 snapshot. I can show the SQL.",
    "St-Hubert Group Inc. has the largest single facility commitment among them, $60.3M.\n\n"
    "Basis: facilities joined to accounts, country = 'Canada'. USD. I can show the SQL.",
)


async def run_demo(output_dir: Path) -> list[dict[str, Any]]:
    """Write a trace and return a checkpoint snapshot after each of two turns on one thread.

    Only the model's decisions and prose are scripted. The SQL runs on the real engine; identity,
    policy, tool-result framing and the checkpoint use the workspace's own configuration.
    """
    agent_config = replace(
        load_test_config(WORKSPACE / "agents/rmqna/.env.example", state_dir=output_dir / "agent"),
        model_trace_path=output_dir / "model-trace.jsonl",
    )
    server_config = load_test_config(
        WORKSPACE / "mcp-servers/rmqna-data/.env.example", state_dir=output_dir / "server"
    )
    providers = scripted_providers(
        calls_tool("rmqna-data.execute_sql", sql=FIRST_SQL),
        ANSWERS[0],
        calls_tool("rmqna-data.execute_sql", sql=SECOND_SQL),
        ANSWERS[1],
    )
    history: list[dict[str, Any]] = []
    async with ServiceContainer(server_config) as server_services:
        connector = InProcessMcpConnector({SERVER_ID: build_server(server_services)})
        async with ServiceContainer(agent_config, providers, mcp_connector=connector) as services:
            graph = build_graph(services, await services.mcp_tools(SERVER_ID))
            for turn, question in enumerate(QUESTIONS, start=1):
                # Each request is authenticated separately: one conversation, a fresh request ID.
                context = await services.authenticate(
                    None, application=APPLICATION, thread_id="react-demo"
                )
                reply = await answer(graph, services, context, question)
                # Checkpoint inspection also needs the owning caller's scope.
                with bind_request_context(context):
                    snapshot = await graph.aget_state(services.invocation(context)["config"])
                history.append(
                    {
                        "turn": turn,
                        "request_id": context.request_id,
                        "thread_id": "react-demo",
                        "question": question,
                        "answer": reply,
                        "messages": [
                            message.model_dump(mode="json")
                            for message in snapshot.values["messages"]
                        ],
                    }
                )
    return history


async def main() -> None:
    """Run the two turns and print what each model call was sent, and where the trace is."""
    output_dir = ENV_FILE.parent / ".agentcraft2" / "react-demo"
    output_dir.mkdir(parents=True, exist_ok=True)
    history = await run_demo(output_dir)
    for turn in history:
        print(f"Turn {turn['turn']}: {turn['question']}")
        print(turn["answer"])
    print(f"Messages saved after each turn: {[len(turn['messages']) for turn in history]}")
    print(f"Trace: {(output_dir / 'model-trace.jsonl').resolve()}")


if __name__ == "__main__":
    asyncio.run(main())
