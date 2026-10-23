"""Run a scripted memo and follow-up against the supplied CSV snapshot through MCP."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

from ai_agent_lib_core import ServiceContainer
from ai_agent_lib_core.pipeline import bind_request_context
from ai_agent_lib_core.testing import calls_tool, load_test_config, scripted_providers
from ai_agent_lib_core.testing.mcp import InProcessMcpConnector

from rmprep.graph import APPLICATION, answer, build_graph
from rmprep.settings import ENV_FILE
from rmprep_data import SERVER_ID, build_server

WORKSPACE = ENV_FILE.parent.parents[1]
QUESTIONS = (
    "Prepare a meeting memo for JONE-0022 as of 2026-08-03.",
    "Which covenants need discussion for this client?",
)
MEMO = """SCRIPTED EXCERPT - supplied CSV snapshot, not independently verified
Client: Jones Dairy Farm (JONE-0022). Meeting date: 2026-08-03.
Priority discussion: Review the recorded covenant failures and overdue reporting.
The credit snapshot reports DSCR 1.04 and current ratio 0.87.
Source: credit_metrics[uen=JONE-0022]. Ratios can have different effective dates
from the rating date and are distinct from covenant-tested values.
Ask the client to confirm its liquidity plan and the owner of missing reporting.
Data gaps: External news and publication dates for financial facts are not supplied.
"""
FOLLOW_UP = (
    "SCRIPTED ANSWER: The supplied covenants for facility FAC000000130 record two failures. "
    "Verify waiver and cure status with Credit. The next test date does not date the recorded test. "
    "Sources: covenants[facility_id=FAC000000130;name=DSCR] and "
    "covenants[facility_id=FAC000000130;name=Fixed Charge Coverage]."
)


async def run_demo(output_dir: Path) -> list[dict[str, Any]]:
    """Write trace and checkpoint snapshots for two turns on one local thread.

    Only model decisions and prose are scripted. MCP queries, identity, policy,
    tool-result framing and SQLite checkpoints use the actual local providers.
    """
    agent_config = replace(
        load_test_config(WORKSPACE / "agents/rmprep/.env.example", state_dir=output_dir / "agent"),
        model_trace_path=output_dir / "model-trace.jsonl",
    )
    server_config = load_test_config(
        WORKSPACE / "mcp-servers/rmprep-data/.env.example", state_dir=output_dir / "server"
    )
    providers = scripted_providers(
        calls_tool("rmprep-data.get_memo_evidence", uen="JONE-0022", meeting_date="2026-08-03"),
        MEMO,
        calls_tool(
            "rmprep-data.get_client_records",
            uen="JONE-0022",
            section="covenants",
            end_date="2026-08-03",
        ),
        FOLLOW_UP,
    )
    history: list[dict[str, Any]] = []
    async with ServiceContainer(server_config) as server_services:
        connector = InProcessMcpConnector({SERVER_ID: build_server(server_services)})
        async with ServiceContainer(agent_config, providers, mcp_connector=connector) as services:
            await services.validate()
            graph = build_graph(services, await services.mcp_tools(SERVER_ID))
            for question in QUESTIONS:
                context = await services.authenticate(
                    None, application=APPLICATION, thread_id="jone-0022-meeting"
                )
                reply = await answer(graph, services, context, question)
                with bind_request_context(context):
                    snapshot = await graph.aget_state(services.invocation(context)["config"])
                history.append(
                    {
                        "thread_id": context.thread_id,
                        "request_id": context.request_id,
                        "route": snapshot.values["route"],
                        "question": question,
                        "answer": reply,
                        "memos": snapshot.values.get("memos", []),
                        "context_diagnostics": snapshot.values.get("context_diagnostics", {}),
                        "messages": [
                            message.model_dump(mode="json")
                            for message in snapshot.values["messages"]
                        ],
                    }
                )
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "checkpoint-history.json").write_text(
        json.dumps(history, indent=2, ensure_ascii=True) + "\n", encoding="utf-8"
    )
    return history


def main() -> None:
    """Show the scripted answers and where to inspect each model call."""
    folder = ENV_FILE.parent / ".agentcraft2" / "react-demo" / uuid4().hex
    history = asyncio.run(run_demo(folder))
    print("Supplied CSV snapshot; model decisions and answers are scripted.")
    for turn in history:
        print(f"\nThread: {turn['thread_id']} | Route: {turn['route']}")
        print(f"Question: {turn['question']}")
        print(json.dumps(turn["answer"], ensure_ascii=True))
        print(f"Checkpoint: {len(turn['messages'])} messages")
    print(f"\nTrace: {folder / 'model-trace.jsonl'}")
    print(f"Checkpoint history: {folder / 'checkpoint-history.json'}")


if __name__ == "__main__":
    main()
