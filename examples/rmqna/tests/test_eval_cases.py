"""The golden set is sound: every reference statement runs, returns what the case says, and follows
the rules the agent is taught, and no case is one of the prompt's worked examples.

``agents/rmqna/evals/cases.jsonl`` is scored by what the agent's SQL returns against what the case's
``reference_sql`` returns, so a reference that the server refuses, that returns something other than
the case pins, or that depends on a rule the agent was not taught would make the score meaningless.
Everything here runs on the real engine and validator, so a refreshed snapshot fails the case whose
answer moved, with the case named.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from agentcraft2_core.evaluation import EvalCase, load_cases

from rmqna.prompts import EXAMPLES_VERSION, load_prompt, schema_document
from rmqna_data.engine import DEFAULT_ROW_CAP, RmqnaEngine
from rmqna_data.settings import DATA_DIR

ROOT = Path(__file__).resolve().parents[1]
CASES = load_cases(ROOT / "agents" / "rmqna" / "evals" / "cases.jsonl")
BY_ID = {case.id: case for case in CASES}
WITH_SQL = [case for case in CASES if "reference_sql" in case.expected]
WITHOUT_SQL = [case for case in CASES if "reference_sql" not in case.expected]
EXAMPLES = load_prompt("examples", EXAMPLES_VERSION)

ANCHOR = "2026-06-30"
LIST_LIMIT = 20
"""The most rows the agent's own rule lets a list come back with: LIMIT 20 unless asked for more."""

# What every case must cover, and how many cases cover it at least.
COVERAGE = {
    "join": 8,
    "agg": 6,
    "date": 6,
    "entity": 3,
    "empty": 2,
    "ambiguity": 1,
    "refusal": 1,
    "xref": 1,
    "many-row": 1,
}


@pytest.fixture(scope="module")
def engine() -> Iterator[RmqnaEngine]:
    engine = RmqnaEngine(DATA_DIR)
    try:
        yield engine
    finally:
        engine.close()


def ref(case: EvalCase) -> str:
    return str(case.expected["reference_sql"])


# ---------------------------------------------------------------------------------------------
# The file
# ---------------------------------------------------------------------------------------------


def test_the_golden_set_is_about_thirty_cases_over_every_kind_of_question() -> None:
    assert 30 <= len(CASES) <= 36
    tagged = {tag: sum(tag in case.tags for case in CASES) for tag in COVERAGE}
    assert all(tagged[tag] >= least for tag, least in COVERAGE.items()), tagged


def test_every_case_is_shaped_as_the_scorers_read_it() -> None:
    allowed = {"result", "reference_sql", "answer_contains"}
    for case in CASES:
        assert set(case.input) == {"question"}, case.id
        assert str(case.input["question"]).strip().endswith(("?", ".")), case.id
        assert set(case.expected) <= allowed, case.id
        assert case.tags, case.id
        # A case with a reference states what it returns, and a case without one is a question
        # to clarify or to decline: it is tagged so, and what the answer must say is listed.
        assert ("result" in case.expected) == ("reference_sql" in case.expected), case.id
        assert ("no-sql" in case.tags) == ("reference_sql" not in case.expected), case.id
        if "reference_sql" not in case.expected:
            assert case.expected.get("answer_contains"), case.id
        for entry in case.expected.get("answer_contains", []):
            texts = [entry] if isinstance(entry, str) else entry
            assert texts and all(isinstance(text, str) and text for text in texts), case.id


def test_the_cases_without_sql_are_the_ones_the_agent_must_not_answer_with_sql() -> None:
    assert {case.id for case in WITHOUT_SQL} == {
        "woolen-revenue",
        "clients-at-risk",
        "refuse-delete",
        "refuse-forecast",
        "refuse-contact-list",
    }
    # A clarification is a question; a refusal is one of the ways the prompt says to decline.
    for case in WITHOUT_SQL:
        if "ambiguity" in case.tags:
            assert "?" in case.expected["answer_contains"], case.id
        else:
            assert "refusal" in case.tags, case.id


# ---------------------------------------------------------------------------------------------
# Every reference runs, and returns what its case pins
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("case", WITH_SQL, ids=lambda case: case.id)
async def test_each_reference_runs_on_the_engine_and_returns_what_the_case_says(
    case: EvalCase, engine: RmqnaEngine
) -> None:
    # The engine validates first, so a statement that gets here passes every narrowing of the
    # validator: no PIVOT, generator, set operation or recursive CTE, bare table names, at most 16
    # joined sources per SELECT, 32 joins and 32 CTE and subquery references, and a result under
    # the 2 MB the engine refuses beyond.
    result = await engine.execute(ref(case))

    assert not result.truncated
    # Short enough that the agent's own LIMIT 20 never cuts the list the reference returns.
    assert result.row_count <= LIST_LIMIT
    assert result.row_count <= DEFAULT_ROW_CAP
    pinned = case.expected["result"]
    if "scalar" in pinned:
        assert (result.row_count, len(result.columns)) == (1, 1), case.id
        assert result.rows[0][0] == pinned["scalar"], case.id
    else:
        assert result.columns == pinned["columns"], case.id
        assert result.rows == pinned["rows"], case.id
    # A case is tagged empty exactly when its reference returns nothing.
    assert (result.row_count == 0) == ("empty" in case.tags), case.id


@pytest.mark.parametrize(
    "case", [case for case in WITH_SQL if "top-n" in case.tags], ids=lambda case: case.id
)
async def test_a_ranking_does_not_end_on_a_tie(case: EvalCase, engine: RmqnaEngine) -> None:
    # Whatever is at the cut is better than what is just beyond it, so the same clients make the
    # list however the agent breaks a tie. The measure is the last column of each ranking.
    limited = re.search(r"\sLIMIT\s+(\d+)\s*$", ref(case))
    assert limited is not None, case.id
    shown = int(limited.group(1))
    everything = await engine.execute(re.sub(r"\sLIMIT\s+\d+\s*$", "", ref(case)))
    assert everything.row_count > shown, case.id
    assert everything.rows[shown - 1][-1] != everything.rows[shown][-1], case.id


# ---------------------------------------------------------------------------------------------
# The references follow the rules the agent is taught
# ---------------------------------------------------------------------------------------------

# What the prompt and the schema document say not to write, as the worked examples' own test spells.
FORBIDDEN = re.compile(
    r"CURRENT_DATE|CURRENT_TIMESTAMP|\bnow\s*\(|\btoday\s*\(|\bUNION\b|\bEXCEPT\b|\bINTERSECT\b"
    r"|\bPIVOT\b|\bUNPIVOT\b|\bRECURSIVE\b|\b(crm|lending|payments|main)\.\w+|'(Yes|No)'",
    re.I,
)
RELATIVE = re.compile(r"\bINTERVAL\b|\bdate_diff\b|\bdate_trunc\b", re.I)


@pytest.mark.parametrize("case", WITH_SQL, ids=lambda case: case.id)
def test_a_reference_follows_the_schema_documents_conventions(case: EvalCase) -> None:
    sql = ref(case)
    assert not FORBIDDEN.search(sql), case.id
    # A window that moves is measured from the snapshot date, written out, never from today.
    if RELATIVE.search(sql):
        assert f"DATE '{ANCHOR}'" in sql, case.id
    # A rating is a word; the number comes from the xref row that has one, once.
    if "xref" in sql:
        assert "TRY_CAST(xref_value AS INT) IS NOT NULL" in sql, case.id
        assert "xref_type = 'risk_rating'" in sql, case.id
    # Booleans are true and false, never the Yes and No of the source system.
    assert not re.search(r"=\s*'(true|false)'", sql, re.I), case.id


def test_the_anchor_the_cases_assume_is_the_schema_documents() -> None:
    assert f"latest month-end is {ANCHOR}" in schema_document()
    # One case states the snapshot date outright; the others that move a window assume it, as an
    # RM would, and their references measure it from that date (checked above).
    assert ANCHOR in str(BY_ID["overdue-deals"].input["question"])
    assert sum(f"DATE '{ANCHOR}'" in ref(case) for case in WITH_SQL) >= 3


async def test_the_names_the_entity_cases_type_resolve_as_the_cases_assume(
    engine: RmqnaEngine,
) -> None:
    async def names(fragment: str) -> list[str]:
        return (await engine.values("accounts", "name", fragment)).values

    # One match each, so the agent filters on it without asking.
    assert await names("canada goose") == ["Canada Goose Holdings Inc."]
    assert await names("prairie meats") == ["Prairie Meats LP"]
    assert await names("sleep country") == ["Sleep Country Canada Inc."]
    # An RM who types "Farm" and means every client with it in its name gets four.
    assert len(await names("farm")) == 4
    # And "Woolen" is two clients, which is the case's ambiguity: both are named in its answer.
    woolen = await names("woolen")
    assert len(woolen) == 2
    needed = BY_ID["woolen-revenue"].expected["answer_contains"]
    assert all(any(text in name for name in woolen) for text in needed if text != "?")


# ---------------------------------------------------------------------------------------------
# No case is one of the prompt's worked examples
# ---------------------------------------------------------------------------------------------


def example_questions() -> list[str]:
    return [
        text.strip() for text in re.findall(r"^Question(?: [A-Z])?: (.+)$", EXAMPLES, flags=re.M)
    ]


def example_statements() -> list[str]:
    return re.findall(r"```sql\n(.*?)```", EXAMPLES, flags=re.S)


def words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9']+", text.lower()))


def squeeze(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).strip().rstrip(";").lower()


def test_the_worked_examples_are_found() -> None:
    # A parser that quietly finds nothing would pass the checks below.
    assert len(example_questions()) == 13  # twelve examples; the last is two questions
    assert len(example_statements()) == 13


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.id)
def test_no_case_asks_a_question_the_prompt_already_answers(case: EvalCase) -> None:
    question = str(case.input["question"])
    for shown in example_questions():
        assert question.lower() != shown.lower(), (case.id, shown)
        # Not a rewording of it either: by the words they share, and by the text they share.
        shared = len(words(question) & words(shown)) / len(words(question) | words(shown))
        similar = difflib.SequenceMatcher(None, question.lower(), shown.lower()).ratio()
        assert shared < 0.5 and similar < 0.7, (case.id, shown, shared, similar)


@pytest.mark.parametrize("case", WITH_SQL, ids=lambda case: case.id)
def test_no_reference_is_a_statement_the_prompt_already_shows(case: EvalCase) -> None:
    shown = {squeeze(sql) for sql in example_statements()}
    assert squeeze(ref(case)) not in shown, case.id
