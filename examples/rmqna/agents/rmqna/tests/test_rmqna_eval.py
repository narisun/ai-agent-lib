"""rmqna with a real model, scored over evals/cases.jsonl.

Unit tests prove the plumbing with a scripted model; this measures the agent.
It runs only when asked, and only with a real model set in the service's .env:

    agentcraft2 eval rmqna

The model is real and live; everything it reaches is not. The `rmqna-data` tools are connected in
process, so there is no server to start, and the engine behind them is the one the real server
builds, over the workspace's data. A case is scored on what the agent's SQL returned, not on its
words: each case's `reference_sql` is run through the same engine, and the two results are compared
by `results_match`. The scorers and the task live in this module, and
`tests/test_eval_harness.py` at the workspace root proves them with a scripted model, so the
plumbing is checked on every CI run even though this test is not.

The report is written to .agentcraft2/eval-report.json, and what the agent said and ran for each case
to .agentcraft2/eval-outputs.json (both private; the second holds the model's answers). Keep the report
with the change that produced it, and raise the bar as the agent gets better.

When evals/baseline.json exists — written by `agentcraft2 eval rmqna --accept` —
the run also fails on regressions against it. `agentcraft2 eval rmqna --accept`
leaves a marker beside the report for this run only, so it skips just that
gate to move the bar on purpose.
"""

from __future__ import annotations

import itertools
import json
import math
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from ai_agent_lib_core import ServiceConfig, ServiceContainer, ServiceProviders
from ai_agent_lib_core.evaluation import (
    EvalCase,
    Scorer,
    dataset_digest,
    load_cases,
    load_report,
    require_no_regression,
    run_eval,
    scored_by,
)
from ai_agent_lib_core.testing import load_eval_config, load_test_config
from ai_agent_lib_core.testing.mcp import InProcessMcpConnector
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langgraph.graph.state import CompiledStateGraph

from rmqna import APPLICATION, build_graph
from rmqna.prompts import EXAMPLES_VERSION, SCHEMA_VERSION, SYSTEM_VERSION
from rmqna_data import SERVER_ID, build_server
from rmqna_data.engine import RmqnaEngine
from rmqna_data.settings import DATA_DIR

SERVICE = Path(__file__).resolve().parents[1]
CASES = SERVICE / "evals" / "cases.jsonl"
REPORT = SERVICE / ".agentcraft2" / "eval-report.json"
OUTPUTS = SERVICE / ".agentcraft2" / "eval-outputs.json"
BASELINE = SERVICE / "evals" / "baseline.json"
ACCEPT_MARKER = SERVICE / ".agentcraft2" / "eval-accept.flag"
SERVER_ENV = SERVICE.parents[1] / "mcp-servers" / "rmqna-data" / ".env.example"

EXECUTE_SQL = "rmqna-data_execute_sql"
"""The name the model API gives the tool: the registered name, made safe for a provider."""

TOLERANCE = 1e-9
"""How close two numbers must be to count as the same figure."""

# ---------------------------------------------------------------------------------------------
# Comparing two results
# ---------------------------------------------------------------------------------------------

Cell = tuple[str, Any]
"""A value after normalizing: its kind, and the value in a form that compares."""


@dataclass(frozen=True, slots=True)
class Table:
    """A result: column names, and rows of values in that order."""

    columns: list[str]
    rows: list[list[Any]]

    @classmethod
    def of(cls, payload: Mapping[str, Any]) -> Table:
        """Read the ``{columns, rows}`` of an ``execute_sql`` result."""
        return cls(list(payload["columns"]), [list(row) for row in payload["rows"]])


_MIDNIGHT = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:[T ]00:00:00(?:\.0+)?(?:Z|\+00:00)?)?$")


def normalize_cell(value: Any) -> Cell:
    """Return a value in the form results are compared in.

    A number is a float, whether it was stored as an integer, a float or a decimal. A date is its
    ISO text, and a timestamp at midnight UTC is the date, so a statement that adds an interval to
    a date and one that does not return the same cell. Text is compared as it is.
    """
    if value is None:
        return ("null", None)
    if isinstance(value, bool):
        return ("bool", value)
    if isinstance(value, int | float | Decimal):
        return ("number", float(value))
    if isinstance(value, datetime | date):
        value = value.isoformat()
    if isinstance(value, str):
        found = _MIDNIGHT.match(value)
        return ("text", found.group(1) if found else value)
    return ("other", json.dumps(value, sort_keys=True, default=str))


def _same_cell(left: Cell, right: Cell) -> bool:
    if left[0] != right[0]:
        return False
    if left[0] == "number":
        return math.isclose(left[1], right[1], rel_tol=TOLERANCE, abs_tol=TOLERANCE)
    return bool(left[1] == right[1])


def _order(cell: Cell) -> Cell:
    # Rows are sorted before they are compared. Numbers are sorted by their first nine
    # significant digits, so two figures that differ only inside the tolerance sort together.
    return (cell[0], float(f"{cell[1]:.9g}")) if cell[0] == "number" else cell


def _same_rows(left: list[list[Cell]], right: list[list[Cell]]) -> bool:
    if len(left) != len(right):
        return False
    ordered_left = sorted(left, key=lambda row: [_order(cell) for cell in row])
    ordered_right = sorted(right, key=lambda row: [_order(cell) for cell in row])
    return all(
        len(a) == len(b) and all(_same_cell(x, y) for x, y in zip(a, b, strict=True))
        for a, b in zip(ordered_left, ordered_right, strict=True)
    )


_LABELS = ("text", "null")
"""The kinds of cell that say what a row is about and are not a figure."""


def _is_lone_figure(wanted: list[list[Cell]]) -> bool:
    return len(wanted) == 1 and len(wanted[0]) == 1


def _stands_alone(figure: Cell, row: list[Cell], at: int) -> bool:
    """Whether ``row[at]`` is the only figure in the row: the rest are labels or the same figure."""
    return all(
        cell[0] in _LABELS or _same_cell(cell, figure)
        for other, cell in enumerate(row)
        if other != at
    )


def _choices(
    index: int,
    name: str,
    wanted: list[list[Cell]],
    columns: Sequence[str],
    got: list[list[Cell]],
) -> tuple[list[int], bool]:
    """The columns of the agent's result that could be the reference's column ``index``.

    A column with the reference's name is the column, whatever it holds. A column the agent named
    differently is the same column when it holds the same values; the second item of the answer is
    False for these, because a name did not bind them.

    A reference that is one cell is evidence of little: any column of a wide row may hold the same
    figure by chance. Such a cell is found by value only in a row with no other figure in it.
    """
    named = [at for at, other in enumerate(columns) if other.lower() == name.lower()]
    if named:
        return named, True
    column = [[row[index]] for row in wanted]
    found = [at for at in range(len(columns)) if _same_rows(column, [[row[at]] for row in got])]
    if _is_lone_figure(wanted):
        figure = wanted[0][index]
        found = [at for at in found if _stands_alone(figure, got[0], at)]
    return found, False


def _in_reference_order(chosen: tuple[int, ...], named: Sequence[bool]) -> bool:
    """Whether the columns found by value are in the order the reference has them."""
    by_value = [at for at, bound in zip(chosen, named, strict=True) if not bound]
    return all(earlier < later for earlier, later in itertools.pairwise(by_value))


def results_match(expected: Table, actual: Table) -> bool:
    """Whether the agent's result holds the figures the reference's does.

    This is a match of what the statements returned, not of how they were written:

    * rows are compared as a multiset, in any order, so a repeated row has to be repeated;
    * a column of the reference is the agent's column of the same name, wherever it stands. A
      column the agent named differently (an alias) is matched by its values, and the columns
      matched this way keep the reference's relative order: two aliased figures that are crossed
      (a "before" that holds the later figure) are not the reference's. Columns the reference does
      not have are ignored, because the agent is asked to show the readable name beside an id and
      to say what a figure is;
    * a reference of one cell is found by value only where it is the one figure in its row, so a
      wide row does not match because one of its columns holds the number by chance;
    * numbers are equal within a relative and absolute 1e-9, an integer equals the same float, and a
      date equals the timestamp at midnight UTC that DuckDB makes of it;
    * two empty results match, whatever their columns, and an empty result matches nothing else.
    """
    wanted = [[normalize_cell(cell) for cell in row] for row in expected.rows]
    got = [[normalize_cell(cell) for cell in row] for row in actual.rows]
    if not wanted or not got:
        return not wanted and not got
    if len(expected.columns) > len(actual.columns):
        return False
    options = [
        _choices(index, name, wanted, actual.columns, got)
        for index, name in enumerate(expected.columns)
    ]
    named = [bound for _, bound in options]
    return any(
        len(set(chosen)) == len(chosen)
        and _in_reference_order(chosen, named)
        and _same_rows(wanted, [[row[at] for at in chosen] for row in got])
        for chosen in itertools.product(*(found for found, _ in options))
    )


def expected_table(case: EvalCase, reference: Table) -> Table:
    """The table a case wants: what its reference statement returned.

    A case whose pinned result is a scalar wants one cell, the reference's first. It keeps the
    reference's column name, so an agent that used the name is held to it, and one that aliased
    the figure is found by its value, where it stands alone in its row (see :func:`_choices`).
    """
    if "scalar" in case.expected.get("result", {}) and reference.rows:
        return Table(reference.columns[:1], [[reference.rows[0][0]]])
    return reference


# ---------------------------------------------------------------------------------------------
# What the agent did
# ---------------------------------------------------------------------------------------------

_FRAME = re.compile(r'^<untrusted_tool_result tool="[^"]*">\n(.*?)\n</untrusted_tool_result>', re.S)


def tool_payload(message: ToolMessage) -> Any:
    """Return the JSON a tool sent back, out of the frame that marks it as data; None if not JSON."""
    text = str(message.text)
    framed = _FRAME.match(text)
    try:
        return json.loads(framed.group(1) if framed else text)
    except json.JSONDecodeError:
        return None


def observe(messages: Sequence[BaseMessage]) -> dict[str, Any]:
    """Return what a run did, read from the messages the graph kept.

    * ``answer``: the last message's text.
    * ``statements``: the SQL of every ``execute_sql`` call, run or refused.
    * ``sql_calls``: how many there were.
    * ``executions``: the result of every statement that ran, in order (``columns``, ``rows``,
      ``truncated``); a refused statement or a lookup is not one.
    * ``executed_rows``: the last of those, or None. It is what the agent's answer is read from.
    """
    statements = [
        str(call["args"].get("sql", ""))
        for message in messages
        if isinstance(message, AIMessage)
        for call in message.tool_calls
        if call["name"].replace(".", "_") == EXECUTE_SQL
    ]
    executions: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, ToolMessage):
            continue
        payload = tool_payload(message)
        if isinstance(payload, dict) and "rows" in payload and "columns" in payload:
            executions.append(
                {
                    "columns": payload["columns"],
                    "rows": payload["rows"],
                    "truncated": bool(payload.get("truncated", False)),
                }
            )
    return {
        "answer": str(messages[-1].text) if messages else "",
        "statements": statements,
        "sql_calls": len(statements),
        "executions": executions,
        "executed_rows": executions[-1] if executions else None,
    }


@asynccontextmanager
async def in_process_agent(
    agent_config: ServiceConfig,
    server_config: ServiceConfig,
    providers: ServiceProviders | None = None,
) -> AsyncIterator[tuple[ServiceContainer, CompiledStateGraph[Any, Any, Any, Any]]]:
    """Start the agent with the tools of ``rmqna-data`` connected in process.

    The server is the real one, built by ``build_server`` over the workspace's data, with its own
    configuration; no port is opened. ``providers`` is None for a real model, or scripted ones.
    """
    async with ServiceContainer(server_config) as server_services:
        connector = InProcessMcpConnector({SERVER_ID: build_server(server_services)})
        async with ServiceContainer(agent_config, providers, mcp_connector=connector) as services:
            yield services, build_graph(services, await services.mcp_tools(SERVER_ID))


def agent_task(
    services: ServiceContainer, graph: CompiledStateGraph[Any, Any, Any, Any]
) -> Callable[[EvalCase], Awaitable[dict[str, Any]]]:
    """Return the task ``run_eval`` runs for each case: ask the agent, report what it did."""

    async def ask(case: EvalCase) -> dict[str, Any]:
        # A conversation of its own for every case, so no case sees another.
        context = await services.authenticate(
            None, application=APPLICATION, thread_id=f"eval-{case.id}"
        )
        result = await graph.ainvoke(
            {"messages": [HumanMessage(str(case.input["question"]))]},
            **services.invocation(context),
        )
        return observe(result["messages"])

    return ask


# ---------------------------------------------------------------------------------------------
# Scorers
# ---------------------------------------------------------------------------------------------

Reference = Callable[[str], Awaitable[Table]]
"""Runs a statement and returns its result: the engine, for an eval."""


def engine_reference(engine: RmqnaEngine) -> Reference:
    """Return a reference runner over ``engine``, which refuses what the server would refuse."""

    async def run(sql: str) -> Table:
        result = await engine.execute(sql)
        return Table(result.columns, result.rows)

    return run


def result_match(reference: Reference) -> Scorer:
    """Score 1 when the agent ran the statement the case asks for, by what it returned.

    * A case with a ``reference_sql`` passes when the last statement that ran for the agent returned
      what the reference does (:func:`results_match`). A reference that returns nothing is met by
      any statement that returned nothing: the agent is told to look once more at an empty result,
      so its last statement is not the empty one.
    * A case without one is a question to clarify or to decline: it passes when the agent sent no
      SQL at all, run or refused.
    """
    cache: dict[str, Table] = {}

    async def judge(case: EvalCase, output: Any) -> bool:
        sql = case.expected.get("reference_sql")
        if sql is None:
            return bool(output["sql_calls"] == 0)
        if sql not in cache:
            cache[sql] = await reference(str(sql))
        wanted = expected_table(case, cache[sql])
        if not cache[sql].rows:
            return any(not run["rows"] for run in output["executions"])
        last = output["executed_rows"]
        return last is not None and not last["truncated"] and results_match(wanted, Table.of(last))

    return scored_by("result_match", judge)


def _plain(text: str) -> str:
    return (
        text.lower()
        .replace("\N{RIGHT SINGLE QUOTATION MARK}", "'")
        .replace("\N{LEFT SINGLE QUOTATION MARK}", "'")
    )


def mentions(case: EvalCase, output: Any) -> bool:
    """The answer holds everything the case's ``answer_contains`` lists.

    Each entry is a text that must appear, ignoring case and curly quotes; an entry that is a list
    is a set of alternatives, of which one must. A case that lists nothing passes.
    """
    answer = _plain(str(output["answer"]))
    for entry in case.expected.get("answer_contains", []):
        alternatives = [entry] if isinstance(entry, str) else entry
        if not any(_plain(str(text)) in answer for text in alternatives):
            return False
    return True


def scorers(reference: Reference) -> list[Scorer]:
    """Both scorers of the eval: what the SQL returned, and what the answer says."""
    return [result_match(reference), scored_by("answer_contains", mentions)]


# ---------------------------------------------------------------------------------------------
# The eval itself
# ---------------------------------------------------------------------------------------------


@pytest.mark.eval
@pytest.mark.enable_socket
async def test_rmqna_meets_its_bar(tmp_path: Path) -> None:
    config = load_eval_config(SERVICE / ".env", state_dir=tmp_path / "agent")
    if config.model.provider == "fake":
        pytest.skip("set a real model provider and model ID in the service's .env")
    server_config = load_test_config(SERVER_ENV, state_dir=tmp_path / "server")
    engine = RmqnaEngine(DATA_DIR)
    try:
        async with in_process_agent(config, server_config) as (services, graph):
            await services.validate()
            ask = agent_task(services, graph)
            outputs: dict[str, Any] = {}

            async def task(case: EvalCase) -> dict[str, Any]:
                saw = await ask(case)
                outputs[case.id] = saw
                return saw

            report = await run_eval(
                task,
                load_cases(CASES),
                scorers(engine_reference(engine)),
                case_timeout=180.0,
                metadata={
                    "application": APPLICATION,
                    "model": f"{config.model.provider}:{config.model.model_id or ''}",
                    "dataset": dataset_digest(CASES),
                    "prompt": (
                        f"system_v{SYSTEM_VERSION} examples_v{EXAMPLES_VERSION} "
                        f"schema_v{SCHEMA_VERSION}"
                    ),
                },
            )
    finally:
        engine.close()
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(report.to_json(), encoding="utf-8")
    OUTPUTS.write_text(json.dumps(outputs, indent=2, default=str), encoding="utf-8")
    print("\n".join(report.summary()))
    if BASELINE.exists() and not ACCEPT_MARKER.exists():
        # A passing run may still be worse than the last accepted one. agentcraft2
        # eval --accept leaves ACCEPT_MARKER to move the bar on purpose.
        require_no_regression(report, load_report(BASELINE), tolerance=0.05)
    # Starting bars, set below what a first live run is expected to reach. Raise them to just under
    # what an accepted run scores, so a prompt change that costs answers fails here.
    report.require({"result_match": 0.5, "answer_contains": 0.8})
