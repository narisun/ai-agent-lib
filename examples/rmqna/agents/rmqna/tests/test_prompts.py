"""The agent's prompt: versioned package resources, and what the assembled prompt must say.

What the prompt says about the server (its error kinds, its limits, what it refuses) is checked
against the server's own constants in the workspace's ``tests/test_prompt_contract.py``; here the
prompt is checked for what an RM-facing agent must promise and for how it is put together.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from rmqna.prompts import (
    EXAMPLES_VERSION,
    SCHEMA_VERSION,
    SYSTEM_VERSION,
    load_prompt,
    schema_document,
    system_prompt,
)

SNAPSHOT = "2026-06-30"


def test_prompt_versions_are_explicit_package_resources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Read by file name from the package, whatever the working directory is.
    monkeypatch.chdir(tmp_path)
    assert (SYSTEM_VERSION, SCHEMA_VERSION, EXAMPLES_VERSION) == (1, 1, 1)
    assert load_prompt("schema", SCHEMA_VERSION) == schema_document()
    assert load_prompt("system", SYSTEM_VERSION).startswith("You are rmqna")
    with pytest.raises(FileNotFoundError):
        load_prompt("system", 99)


@pytest.mark.parametrize(
    ("kind", "version"),
    [("../x", 1), ("x/y", 1), ("System", 1), ("", 1), ("system", 0), ("system", True)],
)
def test_prompt_resource_names_cannot_escape_the_package(kind: str, version: int) -> None:
    with pytest.raises(ValueError, match="kind"):
        load_prompt(kind, version)


def test_the_prompt_is_the_instructions_then_the_schema_then_the_examples() -> None:
    prompt = system_prompt()
    instructions = load_prompt("system", SYSTEM_VERSION)
    examples = load_prompt("examples", EXAMPLES_VERSION)
    schema_at = prompt.index(schema_document())
    examples_at = prompt.index(examples)

    assert prompt.startswith(instructions)
    # The schema is the large, never-changing middle; the examples sit last, nearest the question.
    assert 0 < len(instructions) < schema_at < examples_at
    assert prompt.count(schema_document()) == 1
    assert prompt.count(examples) == 1
    # The same text on every call: a cached prefix stays valid for as long as the version does.
    assert system_prompt() == prompt


def test_the_instructions_name_the_tools_as_the_model_sees_them() -> None:
    instructions = load_prompt("system", SYSTEM_VERSION)
    for tool in (
        "rmqna-data_execute_sql",
        "rmqna-data_search_column_values",
        "rmqna-data_get_schema",
    ):
        assert tool in instructions


def test_the_instructions_set_the_flow_the_agent_follows() -> None:
    instructions = load_prompt("system", SYSTEM_VERSION)
    for policy in [
        "multiple-choice",  # the clarify gate: one question, lettered options
        "search_column_values",  # names are resolved before they are filtered on
        "at most two repairs",  # the repair loop is bounded
        "error_kind",  # and reads what the server says
        "re-examine",  # an empty result is checked once...
        "naming the filters",  # ...then reported with its caveat, never a bare "none"
        "Basis:",  # tables, filters, as-of
        "offer the SQL",
        "read-only",  # a request to change data is declined
        "<untrusted_tool_result>",  # tool results are data, not instructions
    ]:
        assert policy in instructions, policy


def test_the_as_of_anchor_is_the_schema_documents() -> None:
    # The instructions and the examples quote the snapshot date; the schema document computes it
    # from the data. A refreshed snapshot fails here until the prompt resources move to a new
    # version, instead of teaching the model a stale date.
    rule = next(
        line for line in schema_document().splitlines() if line.startswith("- **as-of rule**")
    )
    assert SNAPSHOT in rule
    assert SNAPSHOT in load_prompt("system", SYSTEM_VERSION)
    examples = load_prompt("examples", EXAMPLES_VERSION)
    assert SNAPSHOT in examples
    # No statement anchors a date after the snapshot; a forward window is written as an interval
    # from it, never as a later literal.
    literals = re.findall(r"DATE '(\d{4}-\d\d-\d\d)'", examples)
    assert SNAPSHOT in literals
    assert max(literals) <= SNAPSHOT


def test_the_examples_are_a_dozen_at_most_and_none_is_numbered_twice() -> None:
    examples = load_prompt("examples", EXAMPLES_VERSION)
    numbers = [int(n) for n in re.findall(r"^### Example (\d+)", examples, re.M)]
    assert numbers == list(range(1, len(numbers) + 1))
    assert 10 <= len(numbers) <= 12


def test_the_examples_never_use_what_the_server_refuses_or_the_schema_forbids() -> None:
    # The statements that are meant to be refused are labelled and tested to be refused in the
    # workspace's tests/test_examples.py; this keeps the rest of the prose honest.
    sql = "\n".join(
        re.findall(r"```sql\n(.*?)```", load_prompt("examples", EXAMPLES_VERSION), re.S)
    )
    for forbidden in ["CURRENT_DATE", "now()", "UNION", "PIVOT", "RECURSIVE", "crm.", "main."]:
        assert forbidden not in sql, forbidden
