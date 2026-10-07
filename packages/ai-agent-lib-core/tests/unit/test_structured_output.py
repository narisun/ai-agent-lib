"""Structured output: strict JSON, Draft 2020-12, validated inside the model pipeline."""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import BaseModel, ConfigDict

from ai_agent_lib_core import StructuredOutput, bind_request_context
from ai_agent_lib_core.adapters import FakeChatModelProvider
from ai_agent_lib_core.contracts import (
    AuditOutcome,
    GuardrailPoint,
    Principal,
    RequestContext,
    ValidationFailed,
)
from ai_agent_lib_core.testing import Fakes

SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "Account answer",
    "type": "object",
    "properties": {
        "account": {"type": "string", "pattern": "^[0-9]{4}$"},
        "balance": {"type": "number", "minimum": 0},
        "as_of": {"type": "string", "format": "date"},
        "tags": {"type": "array", "prefixItems": [{"const": "retail"}], "items": False},
    },
    "required": ["account", "balance"],
    "additionalProperties": False,
}


class AccountAnswer(BaseModel):
    """The balance of one account."""

    model_config = ConfigDict(extra="forbid")

    account: str
    balance: float
    as_of: dt.date | None = None


CONTEXT = RequestContext(
    principal=Principal(subject="u-1", tenant="t-1"),
    application="accounts-agent",
    request_id="r-1",
    thread_id="th-1",
)
QUESTION = [HumanMessage("What is the balance of account 4411?")]


def answering(**arguments: object) -> AIMessage:
    return AIMessage(
        content="", tool_calls=[{"name": "Account_answer", "args": arguments, "id": "c-1"}]
    )


# ----------------------------------------------------------------- the schema


def test_a_matching_value_is_returned_and_a_model_class_gives_an_instance() -> None:
    value = {"account": "4411", "balance": 12.5, "as_of": "2026-03-01", "tags": ["retail"]}
    assert StructuredOutput(SCHEMA).validate(value) == value
    answer = StructuredOutput(AccountAnswer).validate({"account": "4411", "balance": 12.5})
    assert answer == AccountAnswer(account="4411", balance=12.5)


@pytest.mark.parametrize(
    ("value", "place"),
    [
        ({"balance": 1}, r"\$: required"),
        ({"account": "44", "balance": 1}, r"\$\.account: pattern"),
        ({"account": "4411", "balance": -1}, r"\$\.balance: minimum"),
        ({"account": "4411", "balance": "12"}, r"\$\.balance: type"),
        ({"account": "4411", "balance": 1, "note": "x"}, r"\$: additionalProperties"),
        ({"account": "4411", "balance": 1, "as_of": "yesterday"}, r"\$\.as_of: format"),
        ({"account": "4411", "balance": 1, "tags": ["retail", "x"]}, r"\$\.tags: items"),
        ([1, 2], r"\$: type"),
    ],
)
def test_a_value_that_does_not_match_is_refused_by_place_not_by_content(
    value: object, place: str
) -> None:
    with pytest.raises(ValidationFailed, match=place) as caught:
        StructuredOutput(SCHEMA).validate(value)
    assert "yesterday" not in str(caught.value)
    assert "note" not in str(caught.value)


def test_many_problems_are_summarised() -> None:
    schema = {
        "type": "object",
        "properties": {name: {"type": "integer"} for name in "abcdefgh"},
    }
    with pytest.raises(ValidationFailed, match="and 3 more"):
        StructuredOutput(schema).validate(dict.fromkeys("abcdefgh", "text"))


def test_a_model_class_adds_its_own_validation() -> None:
    class Even(BaseModel):
        number: int

        def model_post_init(self, context: object) -> None:
            if self.number % 2:
                raise ValueError("odd")

    with pytest.raises(ValidationFailed, match="does not match the model"):
        StructuredOutput(Even).validate({"number": 3})


@pytest.mark.parametrize(
    "text",
    [
        'Here you go: {"account": "4411", "balance": 1}',
        '```json\n{"account": "4411", "balance": 1}\n```',
        '{"account": "4411", "balance": 1} trailing',
        '{"account": "4411", "balance": NaN}',
        '{"account": "4411", "balance": Infinity}',
        '{"account": "4411", "account": "4412", "balance": 1}',
        "{'account': '4411', 'balance': 1}",
        "",
        "[" * 100_000,
    ],
)
def test_only_strict_json_is_parsed(text: str) -> None:
    with pytest.raises(ValidationFailed, match="not strict JSON"):
        StructuredOutput(SCHEMA).parse(text)


def test_strict_json_is_parsed_then_validated() -> None:
    output = StructuredOutput(SCHEMA)
    assert output.parse(' {"account": "4411", "balance": 1}\n') == {"account": "4411", "balance": 1}
    with pytest.raises(ValidationFailed, match="does not match the schema"):
        output.parse('{"account": "4411"}')


@pytest.mark.parametrize(
    ("schema", "problem"),
    [
        ({"type": "object", "properties": {"a": {"type": "wibble"}}}, "not valid Draft 2020-12"),
        ({"type": "array", "items": {"type": "string"}}, 'must have "type": "object"'),
        ({"properties": {}}, 'must have "type": "object"'),
        (
            {"type": "object", "properties": {"a": {"$ref": "https://example.test/a.json"}}},
            "only refer to its own definitions",
        ),
    ],
)
def test_an_unusable_schema_is_refused_when_it_is_declared(
    schema: dict[str, Any], problem: str
) -> None:
    with pytest.raises(ValueError, match=problem):
        StructuredOutput(schema)
    with pytest.raises(TypeError):
        StructuredOutput("not a schema")  # type: ignore[arg-type]


def test_local_references_are_allowed_and_the_schema_is_copied() -> None:
    schema = {
        "type": "object",
        "properties": {"owner": {"$ref": "#/$defs/person"}},
        "$defs": {"person": {"type": "object", "required": ["name"]}},
    }
    output = StructuredOutput(schema, name="owner record!")
    schema["properties"] = {}
    with pytest.raises(ValidationFailed, match=r"\$\.owner: required"):
        output.validate({"owner": {}})
    output.schema["type"] = "array"
    assert output.schema["type"] == "object"
    assert output.name == "owner_record_"
    assert repr(output) == "StructuredOutput(name='owner_record_')"


def test_the_tool_definition_offers_the_schema_under_a_safe_name() -> None:
    definition = StructuredOutput(AccountAnswer).tool_definition()
    assert definition["type"] == "function"
    assert definition["function"]["name"] == "AccountAnswer"
    assert definition["function"]["description"] == "The balance of one account."
    assert definition["function"]["parameters"]["required"] == ["account", "balance"]
    assert StructuredOutput(SCHEMA).name == "Account_answer"


# --------------------------------------------------------------- the pipeline


async def test_a_governed_model_returns_the_validated_value() -> None:
    fakes = Fakes(model=FakeChatModelProvider([answering(account="4411", balance=1250.0)]))
    async with fakes.container() as services:
        model = services.model().with_structured_output(SCHEMA)
        with bind_request_context(CONTEXT):
            answer = await model.ainvoke(QUESTION)
    assert answer == {"account": "4411", "balance": 1250.0}

    (bound,) = fakes.model.models[0].bound_tools
    assert bound["function"]["name"] == "Account_answer"
    (record,) = fakes.audit.records
    assert record.outcome is AuditOutcome.SUCCESS
    assert record.attributes["output_schema"] == "Account_answer"
    assert record.attributes["output_valid"] is True
    assert "1250" not in str(record.to_dict())


async def test_a_model_class_gives_an_instance_and_raw_can_be_included() -> None:
    reply = AIMessage(
        content="",
        tool_calls=[
            {"name": "AccountAnswer", "args": {"account": "4411", "balance": 9}, "id": "c"}
        ],
    )
    fakes = Fakes(model=FakeChatModelProvider([reply, reply]))
    async with fakes.container() as services:
        with bind_request_context(CONTEXT):
            answer = await services.model().with_structured_output(AccountAnswer).ainvoke(QUESTION)
            both = await (
                services.model()
                .with_structured_output(StructuredOutput(AccountAnswer), include_raw=True)
                .ainvoke(QUESTION)
            )
    assert answer == AccountAnswer(account="4411", balance=9)
    assert both["parsed"] == answer
    assert isinstance(both["raw"], AIMessage)


async def test_a_reply_that_does_not_match_fails_the_call_and_is_recorded() -> None:
    fakes = Fakes(model=FakeChatModelProvider([answering(account="4411", balance="a lot")]))
    async with fakes.container() as services:
        model = services.model().with_structured_output(SCHEMA)
        with (
            bind_request_context(CONTEXT),
            pytest.raises(ValidationFailed, match=r"\$\.balance: type"),
        ):
            await model.ainvoke(QUESTION)
    (record,) = fakes.audit.records
    assert record.outcome is AuditOutcome.FAILED
    assert record.error_type == "ValidationFailed"
    assert record.attributes["output_valid"] is False
    assert "a lot" not in str(record.to_dict())


async def test_a_reply_as_text_must_be_strict_json() -> None:
    good = AIMessage(content='{"account": "4411", "balance": 3}')
    chatty = AIMessage(content='Sure! {"account": "4411", "balance": 3}')
    fakes = Fakes(model=FakeChatModelProvider([good, chatty]))
    async with fakes.container() as services:
        model = services.model().with_structured_output(SCHEMA)
        with bind_request_context(CONTEXT):
            assert await model.ainvoke(QUESTION) == {"account": "4411", "balance": 3}
            with pytest.raises(ValidationFailed, match="not strict JSON"):
                await model.ainvoke(QUESTION)


async def test_two_structured_answers_in_one_reply_are_refused() -> None:
    call = {"name": "Account_answer", "args": {"account": "4411", "balance": 1}}
    reply = AIMessage(content="", tool_calls=[{**call, "id": "a"}, {**call, "id": "b"}])
    fakes = Fakes(model=FakeChatModelProvider([reply]))
    async with fakes.container() as services:
        model = services.model().with_structured_output(SCHEMA)
        with bind_request_context(CONTEXT), pytest.raises(ValidationFailed, match="more than one"):
            await model.ainvoke(QUESTION)


async def test_output_guardrails_run_before_the_schema_is_checked() -> None:
    fakes = Fakes(model=FakeChatModelProvider([answering(account="4411", balance=1)]))
    async with fakes.container() as services:
        model = services.model().with_structured_output(SCHEMA)
        with bind_request_context(CONTEXT):
            await model.ainvoke(QUESTION)
    assert fakes.guardrails.checked[-1] == (
        GuardrailPoint.MODEL_OUTPUT,
        '{"account": "4411", "balance": 1}',
    )


async def test_an_ordinary_call_is_not_affected() -> None:
    fakes = Fakes(model=FakeChatModelProvider(["plain text, not JSON"]))
    async with fakes.container() as services:
        with bind_request_context(CONTEXT):
            reply = await services.model().ainvoke(QUESTION)
    assert reply.content == "plain text, not JSON"
    assert "output_schema" not in fakes.audit.records[0].attributes
