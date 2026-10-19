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
    assert sorted(split_examples(EXAMPLES)) == list(range(1, 13))
    assert len(STATEMENTS) >= 12


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


async def test_the_lookups_quote_what_the_server_returns(engine: RmqnaEngine) -> None:
    lookups = LOOKUP.findall(EXAMPLES)
    assert len(lookups) == 3
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
    # The risk-rating join keeps only the number row of xref, once.
    assert "TRY_CAST(xref_value AS INT) IS NOT NULL" in sql
    assert sql.count("xref_type = 'risk_rating'") == 1
    # Latest means the greatest date per key, never today's date.
    assert "max(as_of) OVER (PARTITION BY facility_id)" in sql
    assert "CURRENT_DATE" not in sql
    # The benchmark join is the calculated one, by integer division.
    assert "a.naics_code // 10" in sql
    # Open pipeline is the four open stages, spelled as stored.
    assert sql.count("IN ('Prospecting', 'Qualification', 'Proposal', 'Negotiation')") == 3
    # A name that was looked up is filtered on whole, never on a pattern.
    assert "a.name = 'Wawona Packing Co., LLC'" in sql
    assert "LIKE" not in sql.upper()
