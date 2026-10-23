"""The prompt's worked examples are true: each statement runs on the real engine, as the prompt says.

A few-shot the server refuses would teach the model a refused pattern, and one that quotes a result
the server does not give would teach it to expect the wrong thing. So every statement in
``examples_v1.md`` is run through the same validator and engine the server uses, and each
lookup and error payload the examples quote is compared with what the engine really returns.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator

import pytest

from rmqna.prompts import EXAMPLES_VERSION, load_prompt
from rmqna_data.engine import RmqnaEngine
from rmqna_data.settings import DATA_DIR
from rmqna_data.validate import SqlRefused

EXAMPLES = load_prompt("examples", EXAMPLES_VERSION)

SQL_BLOCK = re.compile(r"```sql\n(.*?)```", re.S)
LOOKUP = re.compile(
    r'`search_column_values\(table="(\w+)", column="(\w+)", search="([^"]*)"\)` '
    r"returns `(\{.*?\})`"
)
ERROR = re.compile(r'`execute_sql` returns `(\{"error_kind".*\})`\.$', re.M)

# What an example says its statement returns, where it is not simply a result with rows:
# (example, position of the statement) -> the error_kind the server answers with, or "empty".
EXPECTED_OUTCOME = {
    (10, 0): "execution_error",  # the guessed column, which the example repairs
    (11, 0): "empty",  # the latest cycle of a client that has no working-capital rows
}

# What each statement that returns rows returns: its row count and its first row, which the
# example's own answer is written from. A refreshed snapshot, or an edit to a statement that
# changes what it selects, fails here until the example's answer is checked against it.
EXPECTED_RESULT: dict[tuple[int, int], tuple[int, list[object]]] = {
    (1, 0): (4, ["Jones Dairy Farm", "FAC000000130", "DSCR", ">= 1.25", 1.08]),
    (2, 0): (3, ["Wawona Packing Co., LLC", 13940690.0, 15841694.0, 88.0]),
    (3, 0): (10, ["Wawona Packing Co., LLC", 75.4, 88.0, 12.6]),
    (4, 0): (3, ["Revolving Credit", 1, 4862084.0, 777933.0]),
    (5, 0): (6, ["D.G. Yuengling & Son, Inc.", 44877614.0, 46771637.0]),
    (6, 0): (
        1,
        [
            "Revolving Credit Facility \u2014 working capital",
            "Revolving Credit",
            "Proposal",
            38007299.0,
            57,
            "2026-11-28",
        ],
    ),
    (7, 0): (6, ["Freshii Inc.", "Excellent", 1, "Good", 3]),
    (8, 0): (5, ["USD", 1252, 622602860.0]),
    (9, 0): (14, ["McCownGordon Construction, LLC", 1.19, 1.87]),
    (10, 1): (5, ["Faribault Woolen Mill Co.", "FAC000000120", 61981786.0, 32617300.0]),
    (11, 1): (1, [0, 9, 46]),
}

# The columns that hold money, which the instructions say to round to whole units.
MONEY = {
    "drawn",
    "committed",
    "open_pipeline",
    "exposure",
    "amount",
    "commitment",
    "our_hold",
    "pipeline_usd",
    "weighted_pipeline_usd",
    "total_value",
}


def is_whole(cell: object) -> bool:
    """Whether a money cell is a whole number of units; a NULL (no figure) is not money."""
    return cell is None or (isinstance(cell, int | float) and cell == round(cell))


def split_examples(text: str) -> dict[int, str]:
    """Each example's text, by its number."""
    parts = re.split(r"^### Example (\d+)\b.*$", text, flags=re.M)
    return {int(number): body for number, body in zip(parts[1::2], parts[2::2], strict=True)}


STATEMENTS = [
    pytest.param(number, position, sql.strip(), id=f"example-{number}-statement-{position}")
    for number, body in split_examples(EXAMPLES).items()
    for position, sql in enumerate(SQL_BLOCK.findall(body))
]


@pytest.fixture(scope="module")
def engine() -> Iterator[RmqnaEngine]:
    engine = RmqnaEngine(DATA_DIR)
    try:
        yield engine
    finally:
        engine.close()


def test_the_examples_are_found() -> None:
    # A parser that quietly finds nothing would pass every test below.
    numbers = sorted(split_examples(EXAMPLES))
    assert numbers == list(range(1, len(numbers) + 1))
    # Every fenced statement was found, and each one is accounted for below: it either returns
    # rows that are pinned, or is one of the two the examples say returns an error or nothing.
    assert len(STATEMENTS) == EXAMPLES.count("```sql")
    assert {(p.values[0], p.values[1]) for p in STATEMENTS} == EXPECTED_RESULT.keys() | (
        EXPECTED_OUTCOME.keys()
    )


@pytest.mark.parametrize(("number", "position", "sql"), STATEMENTS)
async def test_each_statement_runs_as_its_example_says(
    engine: RmqnaEngine, number: int, position: int, sql: str
) -> None:
    outcome = EXPECTED_OUTCOME.get((number, position), "rows")
    if outcome not in {"rows", "empty"}:
        with pytest.raises(SqlRefused) as refused:
            await engine.execute(sql)
        assert refused.value.error_kind == outcome
        return
    result = await engine.execute(sql)
    # The examples show whole answers, so none is cut at the row cap.
    assert not result.truncated
    assert (result.row_count == 0) == (outcome == "empty")
    if outcome == "rows":
        # What the example's answer was written from: the count and the first row.
        assert (result.row_count, result.rows[0]) == EXPECTED_RESULT[(number, position)]
    # The instructions' own rules hold in the examples: a statement that returns rows rather than
    # a figure has a LIMIT of at most 20, and money is rounded to whole units.
    if result.row_count > 1:
        limit = re.search(r"\bLIMIT (\d+)\b", sql)
        assert limit is not None and int(limit.group(1)) <= 20
    for index, column in enumerate(result.columns):
        if column in MONEY:
            assert all(is_whole(row[index]) for row in result.rows), column


async def test_the_lookups_quote_what_the_server_returns(engine: RmqnaEngine) -> None:
    lookups = LOOKUP.findall(EXAMPLES)
    # Every lookup the examples show quotes its result, and none was missed by the pattern.
    assert lookups
    assert len(lookups) == EXAMPLES.count("`search_column_values(")
    for table, column, search, quoted in lookups:
        found = await engine.values(table, column, search)
        assert json.loads(quoted) == found.to_payload()


async def test_the_repair_example_quotes_the_servers_own_error(engine: RmqnaEngine) -> None:
    body = split_examples(EXAMPLES)[10]
    quoted = ERROR.search(body)
    assert quoted is not None
    first, repaired = (sql.strip() for sql in SQL_BLOCK.findall(body))
    with pytest.raises(SqlRefused) as refused:
        await engine.execute(first)
    # Verbatim, including the engine's own line and caret, so the model sees what it will get.
    assert json.loads(quoted.group(1)) == refused.value.to_payload()
    # The repair changes the column the message names, and nothing else that matters.
    assert "committed_amount" in first
    assert "commitment" in repaired
    assert (await engine.execute(repaired)).row_count == 5


def test_the_examples_follow_the_schema_documents_conventions() -> None:
    sql = "\n".join(SQL_BLOCK.findall(EXAMPLES))
    # Booleans are true and false, as stored.
    assert "in_compliance = false" in sql
    # Every use of the risk-rating vocabulary keeps only the number row of xref.
    assert "xref_type = 'risk_rating'" in sql
    assert sql.count("xref_type = 'risk_rating'") == sql.count(
        "TRY_CAST(xref_value AS INT) IS NOT NULL"
    )
    # Latest means the greatest date per key, never today's date.
    assert "max(as_of) OVER (PARTITION BY facility_id)" in sql
    assert "CURRENT_DATE" not in sql
    # The benchmark join is the calculated one, by integer division.
    assert "a.naics_code // 10" in sql
    # Open pipeline is always the four open stages, spelled as stored, never a part of them.
    assert "'Prospecting'" in sql
    assert sql.count("'Prospecting'") == sql.count(
        "IN ('Prospecting', 'Qualification', 'Proposal', 'Negotiation')"
    )
    # A name that was looked up is filtered on whole, never on a pattern.
    assert "a.name = 'Wawona Packing Co., LLC'" in sql
    assert "LIKE" not in sql.upper()


TABLE_ROW = re.compile(r"^> \| (.*?) \|")


@pytest.mark.parametrize(
    "number",
    [number for number, body in split_examples(EXAMPLES).items() if "> | ---" in body],
)
async def test_an_answers_table_lists_the_rows_in_the_order_the_statement_returned_them(
    engine: RmqnaEngine, number: int
) -> None:
    body = split_examples(EXAMPLES)[number]
    # The answer is built from the last statement of the example (the repaired one, in a repair).
    result = await engine.execute(SQL_BLOCK.findall(body)[-1].strip())
    cells = [
        match.group(1).split(" | ")[0]
        for line in body.splitlines()
        if (match := TABLE_ROW.match(line))
    ]
    header, divider, *shown = cells
    assert divider == "---"
    assert header
    assert shown == [str(row[0]) for row in result.rows]
