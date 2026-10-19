"""The eval's task, scorers and comparison of results, proven with a scripted model.

``agents/rmqna/tests/test_rmqna_eval.py`` measures the agent with a real model, so it runs only when
asked. What it is built from runs here, on every CI run: the task that asks the agent through the
in-process server and reads what it did from the graph's messages, the scorer that runs a case's
reference statement through the engine and compares results, and the table of results that count
as the same and results that do not. The model is scripted; the server, the engine and the
validator are the real ones.

The eval module is loaded by path under a private name. It is a test module of the agent's package,
and pytest's import mode puts neither it nor this workspace's folders on ``sys.path``.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import date, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from ai_agent_lib_core.evaluation import EvalCase, EvalReport, run_eval
from ai_agent_lib_core.pipeline.guardrails import frame_untrusted
from ai_agent_lib_core.testing import (
    calls_tool,
    load_test_config,
    scripted_model,
    scripted_providers,
)
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from rmqna_data.engine import RmqnaEngine
from rmqna_data.settings import DATA_DIR
from rmqna_data.validate import SqlRefused

ROOT = Path(__file__).resolve().parents[1]
AGENT_ENV = ROOT / "agents" / "rmqna" / ".env.example"
SERVER_ENV = ROOT / "mcp-servers" / "rmqna-data" / ".env.example"
EXECUTE = "rmqna-data.execute_sql"


def _eval_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "rmqna_eval_under_test", ROOT / "agents" / "rmqna" / "tests" / "test_rmqna_eval.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve their own module by name
    spec.loader.exec_module(module)
    return module


evals: Any = _eval_module()


def table(columns: list[str], *rows: list[Any]) -> Any:
    return evals.Table(columns, [list(row) for row in rows])


def test_the_eval_finds_its_files() -> None:
    # The real eval cannot run here, so the paths it will use are checked instead.
    assert evals.CASES.is_file()
    assert evals.SERVER_ENV == SERVER_ENV
    assert evals.SERVER_ENV.is_file()
    assert evals.SERVICE == ROOT / "agents" / "rmqna"
    assert evals.SERVICE.joinpath("evals").is_dir()


# ---------------------------------------------------------------------------------------------
# Results that count as the same
# ---------------------------------------------------------------------------------------------

CLIENTS = table(["client", "n"], ["Ada", 1], ["Bob", 2], ["Cy", 3])

SAME = [
    pytest.param(CLIENTS, CLIENTS, id="identical"),
    pytest.param(
        CLIENTS, table(["client", "n"], ["Cy", 3], ["Ada", 1], ["Bob", 2]), id="row-order"
    ),
    pytest.param(
        CLIENTS,
        table(["n", "client"], [1, "Ada"], [2, "Bob"], [3, "Cy"]),
        id="column-order-by-name",
    ),
    pytest.param(
        CLIENTS,
        table(["name", "count"], ["Ada", 1], ["Bob", 2], ["Cy", 3]),
        id="aliased-columns-found-by-their-values",
    ),
    pytest.param(
        CLIENTS,
        table(
            ["uen", "client", "n", "note"],
            ["A-1", "Ada", 1, "x"],
            ["B-2", "Bob", 2, "y"],
            ["C-3", "Cy", 3, "z"],
        ),
        id="extra-columns-are-ignored",
    ),
    pytest.param(table(["n"], [1]), table(["n"], [1.0]), id="integer-equals-float"),
    pytest.param(table(["x"], [0.1 + 0.2]), table(["x"], [0.3]), id="float-noise"),
    pytest.param(table(["x"], [1.0]), table(["x"], [1.0000000005]), id="float-within-tolerance"),
    pytest.param(table(["x"], [1e12]), table(["x"], [1e12 + 100]), id="float-relative-tolerance"),
    pytest.param(table(["d"], ["2026-06-30"]), table(["d"], [date(2026, 6, 30)]), id="date-object"),
    pytest.param(
        table(["d"], ["2026-06-30"]),
        table(["d"], ["2026-06-30T00:00:00"]),
        id="midnight-timestamp-is-the-date",
    ),
    pytest.param(
        table(["d"], [datetime(2026, 6, 30)]),
        table(["d"], ["2026-06-30"]),
        id="datetime-at-midnight",
    ),
    pytest.param(
        table(["a"], [None], ["x"]), table(["a"], ["x"], [None]), id="nulls-sort-with-the-rest"
    ),
    pytest.param(table(["a"], [1], [1]), table(["a"], [1], [1]), id="repeated-rows"),
    pytest.param(table(["a"], [True]), table(["a"], [True]), id="booleans"),
    pytest.param(table(["a"]), table(["b", "c"]), id="both-empty-whatever-the-columns"),
    pytest.param(table([""], [46]), table(["how_many"], [46]), id="a-scalar-is-found-by-value"),
    pytest.param(
        table([""], [46]),
        table(["label", "how_many"], ["clients", 46]),
        id="a-scalar-in-a-wider-row",
    ),
    pytest.param(
        table(["a", "b"], [1, 2], [3, 4]),
        table(["b", "a"], [4, 3], [2, 1]),
        id="two-columns-reordered-and-sorted",
    ),
]

DIFFERENT = [
    pytest.param(
        CLIENTS, table(["client", "n"], ["Ada", 1], ["Bob", 2], ["Cy", 4]), id="one-value-differs"
    ),
    pytest.param(CLIENTS, table(["client", "n"], ["Ada", 1], ["Bob", 2]), id="a-row-is-missing"),
    pytest.param(
        CLIENTS,
        table(["client", "n"], ["Ada", 1], ["Bob", 2], ["Cy", 3], ["Di", 4]),
        id="a-row-is-added",
    ),
    pytest.param(table(["a"], [1], [1]), table(["a"], [1]), id="a-repeated-row-is-dropped"),
    pytest.param(table(["a"], [1]), table(["a"], [1], [1]), id="a-row-is-repeated"),
    pytest.param(CLIENTS, table(["client"], ["Ada"], ["Bob"], ["Cy"]), id="a-column-is-missing"),
    pytest.param(
        table(["a", "b"], [1, 2], [3, 4]),
        table(["a", "b"], [2, 1], [4, 3]),
        id="same-values-in-the-wrong-named-columns",
    ),
    pytest.param(
        table(["a", "b"], [1, 2], [3, 4]),
        table(["a", "b"], [1, 4], [3, 2]),
        id="values-paired-differently",
    ),
    pytest.param(table(["x"], [1.0]), table(["x"], [1.000001]), id="float-beyond-tolerance"),
    pytest.param(table(["n"], [1]), table(["n"], ["1"]), id="text-is-not-a-number"),
    pytest.param(table(["n"], [1]), table(["n"], [True]), id="a-boolean-is-not-a-number"),
    pytest.param(table(["a"], [None]), table(["a"], [""]), id="null-is-not-empty-text"),
    pytest.param(table(["a"], [None]), table(["a"], [0]), id="null-is-not-zero"),
    pytest.param(
        table(["d"], ["2026-06-30"]),
        table(["d"], ["2026-06-30T12:00:00"]),
        id="a-time-of-day-is-not-the-date",
    ),
    pytest.param(table(["d"], ["2026-06-30"]), table(["d"], ["2026-07-01"]), id="another-date"),
    pytest.param(table(["a"], ["Ada"]), table(["a"], ["ada"]), id="text-is-case-sensitive"),
    pytest.param(table(["a"]), table(["a"], [1]), id="nothing-expected-but-a-row"),
    pytest.param(table(["a"], [1]), table(["a"]), id="a-row-expected-but-nothing"),
    pytest.param(table([""], [46]), table(["how_many"], [45]), id="a-scalar-that-is-not-there"),
    pytest.param(
        table([""], [46]), table(["how_many"], [46], [46]), id="a-scalar-repeated-is-two-rows"
    ),
]


@pytest.mark.parametrize(("expected", "actual"), SAME)
def test_results_that_hold_the_same_figures_match(expected: Any, actual: Any) -> None:
    assert evals.results_match(expected, actual)


@pytest.mark.parametrize(("expected", "actual"), DIFFERENT)
def test_results_that_hold_other_figures_do_not(expected: Any, actual: Any) -> None:
    assert not evals.results_match(expected, actual)


def test_a_cell_is_compared_as_what_it_holds() -> None:
    cell = evals.normalize_cell
    assert cell(1) == cell(1.0) == ("number", 1.0)
    assert cell("2026-06-30 00:00:00") == ("text", "2026-06-30")
    assert cell("2026-06-30T00:00:00+00:00") == ("text", "2026-06-30")
    assert cell("2026-06-30T00:00:01") == ("text", "2026-06-30T00:00:01")
    assert cell(None) == ("null", None)
    assert cell(True) == ("bool", True)
    assert cell([1, 2]) == cell([1, 2]) != cell([2, 1])


# ---------------------------------------------------------------------------------------------
# What the agent did, read from the graph's messages
# ---------------------------------------------------------------------------------------------


def framed(tool: str, payload: str) -> ToolMessage:
    return ToolMessage(content=frame_untrusted(tool, payload), tool_call_id=tool)


def call(tool: str, **arguments: Any) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": tool, "args": arguments, "id": f"id-{arguments}"}],
    )


def test_the_last_statement_that_ran_is_what_the_answer_is_read_from() -> None:
    rows = '{"columns": ["n"], "rows": [[1]], "row_count": 1, "truncated": false, "elapsed_ms": 1}'
    last = (
        '{"columns": ["m"], "rows": [[2], [3]], "row_count": 2, "truncated": true, "elapsed_ms": 1}'
    )
    messages: list[BaseMessage] = [
        HumanMessage("How many?"),
        call("rmqna-data_search_column_values", table="accounts", column="name", search="x"),
        framed("rmqna-data.search_column_values", '{"values": ["X"], "truncated": false}'),
        call("rmqna-data_execute_sql", sql="SELECT 1 FROM nowhere"),
        framed("rmqna-data.execute_sql", '{"error_kind": "unknown_table", "message": "no"}'),
        call("rmqna-data_execute_sql", sql="SELECT 1 AS n"),
        framed("rmqna-data.execute_sql", rows),
        call("rmqna-data_execute_sql", sql="SELECT 2 AS m"),
        framed("rmqna-data.execute_sql", last),
        AIMessage("There are two."),
    ]
    seen = evals.observe(messages)

    assert seen["answer"] == "There are two."
    # Every statement the agent sent, run or refused; a lookup is not one.
    assert seen["statements"] == ["SELECT 1 FROM nowhere", "SELECT 1 AS n", "SELECT 2 AS m"]
    assert seen["sql_calls"] == 3
    # Only statements that ran have a result; the refusal and the lookup have none.
    assert [run["columns"] for run in seen["executions"]] == [["n"], ["m"]]
    assert seen["executed_rows"] == {"columns": ["m"], "rows": [[2], [3]], "truncated": True}


def test_a_run_with_no_sql_has_no_result() -> None:
    seen = evals.observe([HumanMessage("Delete it."), AIMessage("I can't change data.")])
    assert seen["sql_calls"] == 0
    assert seen["executed_rows"] is None
    assert seen["executions"] == []
    assert seen["answer"] == "I can't change data."


def test_a_result_that_arrives_without_its_frame_is_read_too() -> None:
    bare = ToolMessage(content='{"columns": ["n"], "rows": [[1]]}', tool_call_id="x")
    seen = evals.observe([call("rmqna-data.execute_sql", sql="SELECT 1"), bare, AIMessage("one")])
    assert seen["sql_calls"] == 1  # the registered name counts as well as the model-safe one
    assert seen["executed_rows"] == {"columns": ["n"], "rows": [[1]], "truncated": False}


# ---------------------------------------------------------------------------------------------
# The task and the scorers, end to end with a scripted model
# ---------------------------------------------------------------------------------------------

SEGMENTS = "SELECT segment, COUNT(*) AS clients FROM accounts GROUP BY segment ORDER BY segment"
NOBODY = "SELECT name FROM accounts WHERE name = 'Nobody'"


def case(case_id: str, reference: str | None, **expected: Any) -> EvalCase:
    wanted: dict[str, Any] = dict(expected)
    if reference is not None:
        wanted["reference_sql"] = reference
    return EvalCase(case_id, {"question": f"Question {case_id}?"}, wanted)


# Each case, and what the scripted agent does for it, in the order the run asks them. The model's
# replies are consumed in order, so the cases run one at a time.
SCENARIOS: list[tuple[EvalCase, list[Any], dict[str, float]]] = [
    (
        case("same-figures", SEGMENTS),
        # Other aliases, another column order, another row order: the same figures.
        [
            calls_tool(
                EXECUTE,
                sql="SELECT COUNT(*) AS how_many, segment AS bank_segment FROM accounts "
                "GROUP BY segment ORDER BY how_many",
            ),
            "Mid-Market has 24.",
        ],
        {"result_match": 1.0, "answer_contains": 1.0},
    ),
    (
        case("other-figures", SEGMENTS),
        [
            calls_tool(
                EXECUTE,
                sql="SELECT segment, COUNT(*) AS clients FROM accounts WHERE country = 'Canada' "
                "GROUP BY segment",
            ),
            "Fewer.",
        ],
        {"result_match": 0.0, "answer_contains": 1.0},
    ),
    (
        case("repaired", SEGMENTS),
        # The first statement is refused; the repair is what the answer is read from.
        [
            calls_tool(EXECUTE, sql="SELECT segment, COUNT(*) AS clients FROM clients GROUP BY 1"),
            calls_tool(EXECUTE, sql=SEGMENTS),
            "Repaired.",
        ],
        {"result_match": 1.0, "answer_contains": 1.0},
    ),
    (
        case("never-ran", SEGMENTS),
        ["Three, I think."],
        {"result_match": 0.0, "answer_contains": 1.0},
    ),
    (
        case("last-statement-decides", SEGMENTS),
        [
            calls_tool(EXECUTE, sql=SEGMENTS),
            calls_tool(EXECUTE, sql="SELECT COUNT(*) AS n FROM accounts"),
            "46 in all.",
        ],
        {"result_match": 0.0, "answer_contains": 1.0},
    ),
    (
        case("scalar", "SELECT COUNT(*) AS clients FROM accounts", result={"scalar": 46}),
        [calls_tool(EXECUTE, sql="SELECT COUNT(*) AS how_many FROM accounts"), "46."],
        {"result_match": 1.0, "answer_contains": 1.0},
    ),
    (
        case("wrong-scalar", "SELECT COUNT(*) AS clients FROM accounts", result={"scalar": 46}),
        [
            calls_tool(EXECUTE, sql="SELECT COUNT(*) AS n FROM accounts WHERE country = 'Canada'"),
            "Eight.",
        ],
        {"result_match": 0.0, "answer_contains": 1.0},
    ),
    (
        case("empty-is-checked-once-more", NOBODY),
        # The empty result is met by the empty statement, though a later one returned a row.
        [
            calls_tool(EXECUTE, sql=NOBODY),
            calls_tool(EXECUTE, sql="SELECT COUNT(*) AS n FROM accounts"),
            "Nobody; checked 46 clients.",
        ],
        {"result_match": 1.0, "answer_contains": 1.0},
    ),
    (
        case("empty-that-never-was", NOBODY),
        [calls_tool(EXECUTE, sql="SELECT name FROM accounts LIMIT 1"), "Someone."],
        {"result_match": 0.0, "answer_contains": 1.0},
    ),
    (
        case("clarified", None, answer_contains=["?", ["Faribault", "Pendleton"]]),
        ["Which one? Faribault Woolen Mill Co. or Pendleton Woolen Mills, Inc."],
        {"result_match": 1.0, "answer_contains": 1.0},
    ),
    (
        case("ran-sql-instead", None, answer_contains=["?"]),
        [calls_tool(EXECUTE, sql="SELECT 1"), "Here is a number."],
        {"result_match": 0.0, "answer_contains": 0.0},
    ),
    (
        case("tried-a-write", None, answer_contains=[["can't", "cannot"]]),
        # A refused write is still SQL the agent should not have sent.
        [
            calls_tool(EXECUTE, sql="DELETE FROM opportunities"),
            "I Can\N{RIGHT SINGLE QUOTATION MARK}t do that.",
        ],
        {"result_match": 0.0, "answer_contains": 1.0},
    ),
]


async def run_scripted(
    tmp_path: Path, scenarios: list[Any]
) -> tuple[EvalReport, dict[str, Any], list[list[BaseMessage]]]:
    """Run the scenarios' cases one at a time; return the report, what the task saw, and the prompts."""
    cases = [scenario[0] for scenario in scenarios]
    replies = [reply for scenario in scenarios for reply in scenario[1]]
    agent_config = load_test_config(AGENT_ENV, state_dir=tmp_path / "agent")
    server_config = load_test_config(SERVER_ENV, state_dir=tmp_path / "server")
    engine = RmqnaEngine(DATA_DIR)
    try:
        async with evals.in_process_agent(
            agent_config, server_config, scripted_providers(*replies)
        ) as (services, graph):
            await services.validate()  # as the real eval does, before it asks anything
            ask = evals.agent_task(services, graph)
            outputs: dict[str, Any] = {}

            async def task(item: EvalCase) -> dict[str, Any]:
                saw: dict[str, Any] = await ask(item)
                outputs[item.id] = saw
                return saw

            report = await run_eval(
                task, cases, evals.scorers(evals.engine_reference(engine)), concurrency=1
            )
            prompts = [list(sent) for sent in scripted_model(services).models[0].calls]
    finally:
        engine.close()
    return report, outputs, prompts


async def test_the_task_and_the_scorers_score_what_the_agents_sql_returned(tmp_path: Path) -> None:
    report, outputs, _ = await run_scripted(tmp_path, SCENARIOS)

    assert report.failed_cases == []
    scored = {
        result.case.id: {score.name: score.value for score in result.scores}
        for result in report.results
    }
    assert scored == {item.id: wanted for item, _, wanted in SCENARIOS}
    assert report.metrics == ("result_match", "answer_contains")

    # What the task read from the graph's messages, for a run that repaired itself and one that
    # ran a second statement: every statement, and the last one that ran.
    repaired = outputs["repaired"]
    assert repaired["sql_calls"] == 2
    assert repaired["statements"][1] == SEGMENTS
    assert len(repaired["executions"]) == 1
    assert repaired["executed_rows"]["columns"] == ["segment", "clients"]
    assert repaired["executed_rows"]["rows"][1] == ["Mid-Market", 24]
    assert outputs["last-statement-decides"]["executed_rows"]["rows"] == [[46]]
    assert outputs["never-ran"]["executed_rows"] is None


async def test_each_case_is_a_conversation_of_its_own(tmp_path: Path) -> None:
    scenarios: list[Any] = [
        (case("first", None), ["One."], {}),
        (case("second", None), ["Two."], {}),
    ]
    report, outputs, prompts = await run_scripted(tmp_path, scenarios)
    assert report.failed_cases == []
    assert outputs["first"]["answer"] == "One."
    assert outputs["second"]["answer"] == "Two."
    # The second case's model is shown its own question and nothing of the first case's.
    asked = [[str(m.content) for m in sent if isinstance(m, HumanMessage)] for sent in prompts]
    assert asked == [["Question first?"], ["Question second?"]]


# ---------------------------------------------------------------------------------------------
# The scorers on their own
# ---------------------------------------------------------------------------------------------


async def no_engine(sql: str) -> Any:
    raise AssertionError(f"no statement should run: {sql}")


def output(**fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "answer": "",
        "statements": [],
        "sql_calls": 0,
        "executions": [],
        "executed_rows": None,
    }
    return base | fields


async def test_a_truncated_result_is_never_a_match() -> None:
    async def reference(sql: str) -> Any:
        return table(["n"], [1], [2])

    run = {"columns": ["n"], "rows": [[1], [2]], "truncated": True}
    scorer = evals.result_match(reference)
    item = case("c", "SELECT n")
    cut = await scorer(item, output(executions=[run], executed_rows=run, sql_calls=1))
    whole = await scorer(
        item,
        output(executions=[run | {"truncated": False}], executed_rows=run | {"truncated": False}),
    )
    assert (cut.value, whole.value) == (0.0, 1.0)


async def test_a_case_with_no_reference_never_runs_a_statement() -> None:
    scorer = evals.result_match(no_engine)
    assert (await scorer(case("c", None), output())).value == 1.0
    assert (await scorer(case("c", None), output(sql_calls=1))).value == 0.0


async def test_a_reference_is_run_once_however_many_cases_share_it() -> None:
    runs: list[str] = []

    async def reference(sql: str) -> Any:
        runs.append(sql)
        return table(["n"], [1])

    scorer = evals.result_match(reference)
    ran = {"columns": ["n"], "rows": [[1]], "truncated": False}
    for name in ("a", "b"):
        score = await scorer(
            case(name, "SELECT 1 AS n"), output(executions=[ran], executed_rows=ran, sql_calls=1)
        )
        assert score.value == 1.0
    assert runs == ["SELECT 1 AS n"]


@pytest.mark.parametrize(
    ("answer", "listed", "passes"),
    [
        pytest.param("Which one do you mean?", ["?"], True, id="a-substring"),
        pytest.param("WHICH ONE?", ["which one"], True, id="ignoring-case"),
        pytest.param(
            "I can\N{RIGHT SINGLE QUOTATION MARK}t do that.",
            [["can't", "cannot"]],
            True,
            id="curly-quote",
        ),
        pytest.param("I cannot.", [["can't", "cannot"]], True, id="any-of-a-list"),
        pytest.param("I will not.", [["can't", "cannot"]], False, id="none-of-a-list"),
        pytest.param("Faribault only", ["Faribault", "Pendleton"], False, id="every-entry-needed"),
        pytest.param("Faribault and Pendleton", ["Faribault", "Pendleton"], True, id="all-present"),
        pytest.param("anything", [], True, id="nothing-listed"),
    ],
)
def test_the_answer_has_what_the_case_lists(answer: str, listed: list[Any], passes: bool) -> None:
    item = case("c", None, answer_contains=listed)
    assert evals.mentions(item, {"answer": answer}) is passes


# ---------------------------------------------------------------------------------------------
# The statements this module runs are the engine's
# ---------------------------------------------------------------------------------------------


async def test_a_reference_the_server_would_refuse_fails_the_run_loudly() -> None:
    engine = RmqnaEngine(DATA_DIR)
    try:
        reference = evals.engine_reference(engine)
        assert (await reference("SELECT COUNT(*) FROM accounts")).rows == [[46]]
        with pytest.raises(SqlRefused) as refused:
            await reference("DROP TABLE accounts")
        assert refused.value.error_kind == "not_a_select"
    finally:
        engine.close()
