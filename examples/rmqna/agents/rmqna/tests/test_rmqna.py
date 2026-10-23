"""The agent's behaviour, tested with the library's fakes.

These are the fastest tests: nothing is real except the graph. The model is scripted with the
replies it should give, and the one tool the agent works with, ``execute_sql``, is a stub that
returns the payloads the rmqna-data server returns. The real server, and what a caller is
allowed to do with it, are exercised by ``tests/test_rmqna_with_rmqna_data.py`` in the workspace.
"""

from __future__ import annotations

import json
from typing import Any

from ai_agent_lib_core import Principal, RequestContext
from ai_agent_lib_core.testing import FakeChatModelProvider, Fakes, calls_tool
from langchain_core.messages import AIMessage
from langchain_core.tools import StructuredTool

from rmqna import APPLICATION, answer, ask, build_graph
from rmqna.prompts import EXAMPLES_VERSION, load_prompt, schema_document, system_prompt

EXECUTE = "rmqna-data.execute_sql"
"""The tool as the registry names it; a model API lists it as ``rmqna-data_execute_sql``."""


def context(subject: str = "u-1", thread: str = "th-1") -> RequestContext:
    return RequestContext(
        principal=Principal(subject=subject, tenant="t-1"),
        application=APPLICATION,
        request_id="r-1",
        thread_id=thread,
    )


class StubServer:
    """Stands in for rmqna-data's ``execute_sql``: each call gets the next canned payload."""

    def __init__(self, *payloads: dict[str, Any]) -> None:
        self._payloads = list(payloads)
        self.statements: list[str] = []

    def tool(self) -> StructuredTool:
        async def execute_sql(sql: str) -> str:
            self.statements.append(sql)
            return json.dumps(self._payloads[len(self.statements) - 1])

        return StructuredTool.from_function(
            coroutine=execute_sql,
            name="rmqna-data_execute_sql",
            description="Run one validated, read-only SQL SELECT over the RM tables.",
        )


BREACHES = (
    "SELECT a.name AS client, c.name AS covenant, c.current_value "
    "FROM covenants c JOIN facilities f ON f.facility_id = c.facility_id "
    "JOIN accounts a ON a.uen = f.uen WHERE c.in_compliance = false ORDER BY a.name"
)
RESULT = {
    "columns": ["client", "covenant", "current_value"],
    "rows": [["Jones Dairy Farm", "DSCR", 1.08], ["Life is Good, Inc.", "Current Ratio", 1.19]],
    "row_count": 2,
    "truncated": False,
    "elapsed_ms": 3,
}
ANSWER = (
    "Two clients have a covenant in breach: Jones Dairy Farm (DSCR 1.08) and Life is Good, Inc. "
    "(Current Ratio 1.19).\n\nBasis: covenants joined to facilities and accounts, in_compliance = "
    "false, as of the 2026-06-30 snapshot. Ask me to show the SQL."
)


def text_of(message: Any) -> str:
    """The text of a message, whether its content is a string or a list of blocks."""
    content = message.content
    if isinstance(content, str):
        return content
    return "".join(block["text"] for block in content if isinstance(block, dict))


async def test_the_agent_executes_sql_and_answers_with_provenance() -> None:
    server = StubServer(RESULT)
    fakes = Fakes(model=FakeChatModelProvider([calls_tool(EXECUTE, sql=BREACHES), ANSWER]))
    async with fakes.container() as services:
        graph = build_graph(services, [server.tool()])
        reply = await answer(graph, services, context(), "Which clients have a covenant in breach?")

    # The model's last message is the answer, passed through untouched.
    assert reply == ANSWER
    assert server.statements == [BREACHES]
    prompts = fakes.model.models[0].calls
    assert len(prompts) == 2
    # Before answering the model was shown the rows the statement selected.
    shown = prompts[1][-1]
    assert shown.type == "tool"
    assert json.loads(text_of(shown)) == RESULT
    # What the answer must state is in the instructions the model was given, on every call.
    for prompt in prompts:
        instructions = text_of(prompt[0])
        assert "Basis:" in instructions
        assert "offer the SQL" in instructions
        assert "2026-06-30" in instructions


async def test_an_error_payload_triggers_one_repair() -> None:
    guess = BREACHES.replace("c.current_value", "c.measured_value")
    error = {
        "error_kind": "execution_error",
        "message": 'Binder Error: Table "c" does not have a column named "measured_value"\n\n'
        'Candidate bindings: : "current_value"',
    }
    server = StubServer(error, RESULT)
    fakes = Fakes(
        model=FakeChatModelProvider(
            [calls_tool(EXECUTE, sql=guess), calls_tool(EXECUTE, sql=BREACHES), ANSWER]
        )
    )
    async with fakes.container() as services:
        graph = build_graph(services, [server.tool()])
        reply = await answer(graph, services, context(), "Which clients have a covenant in breach?")

    assert reply == ANSWER
    # One failure, one repaired statement; the repair is a new call, not a re-send.
    assert server.statements == [guess, BREACHES]
    prompts = fakes.model.models[0].calls
    assert len(prompts) == 3
    # The repair is made with the failed statement and the server's own words in front of the
    # model: the call it made, and the error the server returned for it.
    repair_prompt = prompts[2]
    failed_call, failure = repair_prompt[2], repair_prompt[3]
    assert isinstance(failed_call, AIMessage)
    assert failed_call.tool_calls[0]["args"]["sql"] == guess
    assert json.loads(text_of(failure)) == error


async def test_the_system_prompt_carries_schema_and_examples() -> None:
    fakes = Fakes(model=FakeChatModelProvider(["one", "two"]))
    async with fakes.container() as services:
        graph = build_graph(services)
        await answer(graph, services, context(), "first question")
        await answer(graph, services, context(subject="u-2", thread="th-2"), "second question")

    first = fakes.model.models[0].calls[0][0]
    second = fakes.model.models[0].calls[1][0]
    # The instructions are one static block, marked as the reusable prefix a provider caches.
    assert first.type == "system"
    assert first.additional_kwargs["agentlib_static_prefix"] is True
    assert isinstance(first.content, list)
    assert len(first.content) == 1
    block = first.content[0]
    assert isinstance(block, dict)
    prompt = block["text"]
    assert prompt == system_prompt()
    assert schema_document() in prompt
    assert load_prompt("examples", EXAMPLES_VERSION) in prompt
    # Nothing in it varies by caller, thread or question, so every request shares the cache.
    assert second.content == first.content


async def test_the_agent_remembers_a_conversation_per_caller() -> None:
    fakes = Fakes(model=FakeChatModelProvider(["one", "two", "three"]))
    async with fakes.container() as services:
        await ask(services, context(), "first question")
        await ask(services, context(), "second question")
        await ask(services, context(subject="u-2"), "someone else's question")

    second_prompt, other_prompt = fakes.model.models[1].calls[0], fakes.model.models[2].calls[0]
    assert [m.content for m in second_prompt[1:]] == ["first question", "one", "second question"]
    # The same thread name from another caller is another conversation.
    assert [m.content for m in other_prompt[1:]] == ["someone else's question"]
