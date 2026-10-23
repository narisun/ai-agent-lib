"""A discovered client group survives tools, bounded context and checkpoint restarts."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest
from agentcraft2_core import Principal, ServiceContainer
from agentcraft2_core.pipeline import bind_request_context
from agentcraft2_core.pipeline.guardrails import frame_untrusted
from agentcraft2_core.testing import (
    audit_records,
    calls_tool,
    load_test_config,
    scripted_providers,
)
from agentcraft2_core.testing.mcp import InProcessMcpConnector
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage

from rmprep.clients import client_names, lookup_in_turn, recent_client_lookup
from rmprep.context import ContextBudget, ContextBuilder, estimate_tokens
from rmprep.context_tools import tool_context_issues
from rmprep.examples.trace_react import WORKSPACE
from rmprep.graph import APPLICATION, answer, build_graph
from rmprep.router import router
from rmprep.selection import select_context
from rmprep.state import State
from rmprep_data import SERVER_ID, build_server

CLIENTS = {"CANA-0010": "Canada Goose Holdings Inc.", "SLEE-0041": "Sleep Country Canada Inc."}
SEARCH = 'do we have a client containing "canada"'
COMPARE = "compare these two clients financials"
DISCOVERY = "Canada Goose Holdings Inc. (CANA-0010) and Sleep Country Canada Inc. (SLEE-0041)."
COMPARISON = "Retrieved financial evidence for both listed clients. Sources: " + " ".join(
    f"financial_facts[uen={uen};concept=Revenues;period_end={year}-12-31;statement=income_statement;is_annual=True]"
    for uen in CLIENTS
    for year in (2024, 2025)
)


def search_turn(
    clients: dict[str, str] | None = None, *, truncated: bool = False
) -> list[AnyMessage]:
    clients = CLIENTS if clients is None else clients
    return [
        HumanMessage(SEARCH, id="search-question"),
        AIMessage(
            "",
            id="search-call",
            tool_calls=[
                {"name": "rmprep-data_find_clients", "args": {"search": "canada"}, "id": "search"}
            ],
        ),
        ToolMessage(
            frame_untrusted(
                "rmprep-data.find_clients",
                json.dumps(
                    {
                        "status": "ambiguous" if clients else "not_found",
                        "matches": [{"uen": uen, "name": name} for uen, name in clients.items()],
                        "truncated": truncated,
                    }
                ),
            ),
            name="rmprep-data_find_clients",
            tool_call_id="search",
            id="search-result",
        ),
        AIMessage(DISCOVERY, id="search-answer"),
    ]


def financial_calls(*clients: str) -> AIMessage:
    return AIMessage(
        "",
        tool_calls=[
            {
                "name": "rmprep-data_get_client_records",
                "args": {"uen": uen, "section": "financials"},
                "id": f"financials-{uen}",
            }
            for uen in clients
        ],
    )


def selected_state(history: list[AnyMessage], question: str = COMPARE, **extra: Any) -> State:
    state = cast(State, {"messages": [*history, HumanMessage(question, id="current")], **extra})
    return cast(State, {**state, **router(state)})


def assert_complete_tool_pairs(messages: list[AnyMessage]) -> None:
    calls = {
        call["id"]
        for message in messages
        if isinstance(message, AIMessage)
        for call in message.tool_calls
    }
    results = {message.tool_call_id for message in messages if isinstance(message, ToolMessage)}
    assert calls == results


def test_search_establishes_canonical_names_but_not_an_ambiguous_alias() -> None:
    history = search_turn()
    assert client_names(history, []) == {uen: [name] for uen, name in CLIENTS.items()}
    selection = selected_state(history)["selection"]
    assert selection["client_uens"] == list(CLIENTS)
    assert not selection["uen"] and not selection["memo_ids"]
    assert selection["lookup_message_ids"] == [m.id for m in history]


@pytest.mark.parametrize(
    "question",
    [
        "Compare CANA-0010 and SLEE-0041 financials.",
        "Compare Canada Goose Holdings Inc. and Sleep Country Canada Inc. financials.",
        COMPARE,
        "Compare both.",
        "What is their revenue?",
    ],
)
def test_explicit_and_plural_references_select_both_clients(question: str) -> None:
    selection = selected_state(search_turn(), question)["selection"]
    assert selection["client_uens"] == list(CLIENTS)
    assert not selection["clarification"]
    assert not tool_context_issues(financial_calls(*CLIENTS), selection, [], "2026-10-12")
    assert tool_context_issues(financial_calls("JONE-0022"), selection, [], "2026-10-12")


@pytest.mark.parametrize(
    "history",
    [
        search_turn(truncated=True),
        search_turn({}),
        search_turn({**CLIENTS, "JONE-0022": "Jones Dairy Farm"}),
    ],
)
def test_incomplete_or_non_pair_search_does_not_reuse_stale_active_group(
    history: list[AnyMessage],
) -> None:
    selection = selected_state(history, active_client_uens=list(CLIENTS))["selection"]
    assert selection["clarification"] and not selection["client_uens"]


def test_singular_possessive_and_explicit_client_switch_remain_single_client() -> None:
    selection = select_context(
        "What is their revenue?",
        [],
        "",
        "CANA-0010",
        creating_memo=False,
        active_clients=["CANA-0010"],
    )
    assert selection["client_uens"] == ["CANA-0010"]
    assert tool_context_issues(financial_calls(*CLIENTS), selection, [], "2026-10-12")
    state = selected_state(
        search_turn(), "Show financials for JONE-0022.", active_client_uens=list(CLIENTS)
    )
    assert state["selection"]["client_uens"] == ["JONE-0022"]
    context = ContextBuilder().build(state, "System", state["selection"])
    assert len(context.messages) == 2
    switched = [
        *search_turn(),
        HumanMessage("Show financials for JONE-0022."),
        AIMessage("Jones answer."),
    ]
    assert recent_client_lookup(switched) is None
    assert selected_state(switched, active_client="JONE-0022", active_client_uens=["JONE-0022"])[
        "selection"
    ]["clarification"]


def test_original_lookup_is_pinned_across_long_discussion_and_legacy_failed_answer() -> None:
    history = search_turn()
    for index in range(25):
        history += [
            HumanMessage("Explain the financials.", id=f"human-{index}"),
            AIMessage(
                "Long discussion. " * 100,
                id=f"answer-{index}",
                additional_kwargs={
                    "rmprep_context": {"client_uens": list(CLIENTS), "memo_ids": []}
                },
            ),
        ]
    # The old validator narrowed a failed comparison to the first client.
    history += [
        HumanMessage(COMPARE, id="failed-question"),
        AIMessage(
            "Which second client?",
            id="failed-answer",
            additional_kwargs={"rmprep_context": {"uen": "CANA-0010", "memo_ids": []}},
        ),
    ]
    original = deepcopy(history)
    state = selected_state(history, context_version=2, active_client="CANA-0010")
    context = ContextBuilder(ContextBudget(recent_tokens=250)).build(
        state, "System", state["selection"]
    )
    assert state["selection"]["client_uens"] == list(CLIENTS)
    assert "search-result" in [m.id for m in context.messages]
    assert context.diagnostics["omitted_turns"] == 25
    assert_complete_tool_pairs(context.messages)
    assert history == original


def test_default_budget_keeps_large_comparison_in_a_long_thread_without_splitting_tools() -> None:
    history = search_turn()
    selection = selected_state(history)["selection"]
    for index in range(300):
        history += [
            HumanMessage("Explain the financials. " * 25, id=f"old-question-{index}"),
            AIMessage(
                "Earlier discussion. " * 120,
                id=f"old-answer-{index}",
                additional_kwargs={"rmprep_context": selection},
            ),
        ]
    comparison: list[AnyMessage] = [
        HumanMessage(COMPARE, id="comparison"),
        financial_calls(*CLIENTS),
    ]
    for uen in CLIENTS:
        comparison.append(
            ToolMessage(
                frame_untrusted(
                    "rmprep-data.get_client_records",
                    json.dumps(
                        {
                            "uen": uen,
                            "section": "financials",
                            "records": [{"details": "record detail " * 1600}],
                        }
                    ),
                ),
                name="rmprep-data_get_client_records",
                tool_call_id=f"financials-{uen}",
                id=f"records-{uen}",
            )
        )
    comparison.append(
        AIMessage(
            COMPARISON, id="comparison-answer", additional_kwargs={"rmprep_context": selection}
        )
    )
    assert 6_000 < estimate_tokens(comparison) < 96_000
    history += comparison
    state = selected_state(
        history,
        "Which of these two clients had stronger revenue growth?",
        active_client_uens=list(CLIENTS),
    )
    original = deepcopy(state)
    context = ContextBuilder().build(state, "System", state["selection"])
    ids = {message.id for message in context.messages}
    assert {
        "comparison-answer",
        "records-CANA-0010",
        "records-SLEE-0041",
        "search-result",
        "old-answer-299",
    } <= ids
    assert "old-answer-0" not in ids
    assert cast(int, context.diagnostics["omitted_turns"]) > 0
    assert cast(int, context.diagnostics["retained_recent_tokens"]) <= 96_000
    assert cast(int, context.diagnostics["estimated_input_tokens"]) <= 128_000
    assert_complete_tool_pairs(context.messages)
    assert state == original


@pytest.mark.parametrize("parallel", [True, False])
async def test_discovery_comparison_restart_and_isolation_with_real_mcp_csv(
    tmp_path: Path,
    parallel: bool,
) -> None:
    agent_config = replace(
        load_test_config(WORKSPACE / "agents/rmprep/.env.example", state_dir=tmp_path / "agent"),
        model_trace_path=tmp_path / "trace.jsonl",
    )
    server_config = load_test_config(
        WORKSPACE / "mcp-servers/rmprep-data/.env.example", state_dir=tmp_path / "server"
    )
    async with ServiceContainer(server_config) as server_services:
        connector = InProcessMcpConnector({SERVER_ID: build_server(server_services)})
        discovery = scripted_providers(
            calls_tool("rmprep-data.find_clients", search="canada"), DISCOVERY
        )
        async with ServiceContainer(agent_config, discovery, mcp_connector=connector) as services:
            graph = build_graph(services, await services.mcp_tools(SERVER_ID))
            caller = await services.authenticate(
                None, application=APPLICATION, thread_id="canada-comparison"
            )
            assert await answer(graph, services, caller, SEARCH) == DISCOVERY
            with bind_request_context(caller):
                first = await graph.aget_state(services.invocation(caller)["config"])
            assert set(first.values["active_client_uens"]) == set(CLIENTS)

        decisions = (
            [financial_calls(*CLIENTS)] if parallel else [financial_calls(uen) for uen in CLIENTS]
        )
        providers = scripted_providers(
            *decisions, COMPARISON, "From the saved evidence: " + COMPARISON
        )
        async with ServiceContainer(agent_config, providers, mcp_connector=connector) as services:
            graph = build_graph(services, await services.mcp_tools(SERVER_ID))
            caller = await services.authenticate(
                None, application=APPLICATION, thread_id="canada-comparison"
            )
            assert await answer(graph, services, caller, COMPARE) == COMPARISON
            with bind_request_context(caller):
                saved = await graph.aget_state(services.invocation(caller)["config"])
            assert saved.values["messages"][:4] == first.values["messages"]
            assert saved.values["selection"]["client_uens"] == list(CLIENTS)
            assert saved.values["active_client"] == ""
            assert saved.values["active_client_uens"] == list(CLIENTS)
            assert not saved.values["memos"]
            assert (
                await answer(
                    graph,
                    services,
                    caller,
                    "Which of these two clients had stronger revenue growth?",
                )
                == "From the saved evidence: " + COMPARISON
            )
            with bind_request_context(caller):
                follow_up = await graph.aget_state(services.invocation(caller)["config"])
            assert follow_up.values["messages"][:-2] == saved.values["messages"]
            assert follow_up.values["context_diagnostics"]["recent_token_budget"] == 96_000
            assert_complete_tool_pairs(follow_up.values["messages"])
            for isolated in [
                replace(caller, thread_id="other-thread"),
                replace(
                    caller,
                    principal=Principal(subject="other-user", tenant=caller.principal.tenant),
                ),
            ]:
                assert "Please specify the client names" in await answer(
                    graph, services, isolated, COMPARE
                )
                with bind_request_context(isolated):
                    other = await graph.aget_state(services.invocation(isolated)["config"])
                assert len(other.values["messages"]) == 2

    events = [
        json.loads(line)
        for line in (tmp_path / "trace.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    requests = [event for event in events if event["event"] == "model.request"]
    assert len(requests) == (
        5 if parallel else 6
    )  # Follow-up reuses evidence, without a tool call.
    for event in requests[2:]:
        assert DISCOVERY in [message["content"] for message in event["messages"]]
        assert all(uen in str(event["messages"][0]["content"]) for uen in CLIENTS)
    final_tools = [message for message in requests[-1]["messages"] if message["type"] == "tool"]
    assert len(final_tools) == 3  # Original search plus financials for each client.
    assert all(
        any(uen in str(message["content"]) for message in final_tools[1:]) for uen in CLIENTS
    )
    assert_complete_tool_pairs(saved.values["messages"])
    queries = [
        record for record in audit_records(tmp_path / "server") if record["event"] == "data.query"
    ]
    assert queries and all(record["outcome"] == "success" for record in queries)
    tool_calls = [
        record for record in audit_records(tmp_path / "server") if record["event"] == "tool.call"
    ]
    assert len(tool_calls) == 3  # One search, two financials; none for the follow-up.


def test_lookup_parser_rejects_malformed_result_set_as_incomplete() -> None:
    history = search_turn()
    history[2] = ToolMessage(
        frame_untrusted(
            "rmprep-data.find_clients",
            json.dumps(
                {
                    "status": "ambiguous",
                    "truncated": False,
                    "matches": [{"uen": "CANA-0010", "name": CLIENTS["CANA-0010"]}, {"uen": None}],
                }
            ),
        ),
        name="rmprep-data_find_clients",
        tool_call_id="search",
    )
    lookup = lookup_in_turn(history)
    assert lookup is not None and not lookup.complete
