"""The reference agent, tested offline with the library's fakes and local adapters."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

from accounts_agent import APPLICATION, ask
from accounts_agent.__main__ import main
from accounts_agent.tools import lookup_balance
from ai_agent_lib_core import PolicyDenied, Principal, RequestContext, ServiceContainer
from ai_agent_lib_core.config import ConfigResolver, MappingConfigSource
from ai_agent_lib_core.pipeline import frame_untrusted
from ai_agent_lib_core.testing import FakeChatModelProvider, Fakes, calls_tool

RULES = Path(__file__).parents[1] / "policies" / "agentlib" / "rules" / "data.yaml"


def context(subject: str = "u-1", thread: str = "th-1") -> RequestContext:
    return RequestContext(
        principal=Principal(subject=subject, tenant="t-9"),
        application=APPLICATION,
        request_id="r-1",
        thread_id=thread,
    )


def ask_for(account: str) -> AIMessage:
    return calls_tool("lookup_balance", account=account)


# ---------------------------------------------------------------------- tools


def test_lookup_balance_finds_known_accounts_and_reports_unknown_ones() -> None:
    assert lookup_balance("4411") == "The balance of account 4411 is 1,250.00 USD."
    assert lookup_balance(" 5520 ") == "The balance of account  5520  is 12,004.55 EUR."
    assert lookup_balance("0000") == "No account with number 0000 was found."


# ------------------------------------------------------- the agent, with fakes


async def test_the_agent_looks_the_balance_up_and_answers() -> None:
    fakes = Fakes(model=FakeChatModelProvider([ask_for("4411"), "It is 1,250.00 USD."]))
    async with fakes.container() as services:
        answer = await ask(services, context(), "What is the balance of account 4411?")

    assert answer == "It is 1,250.00 USD."
    prompts = fakes.model.models[0].calls
    assert prompts[0][0].type == "system"
    # The model is shown the tool result marked as data, not as an instruction.
    assert prompts[1][-1].content == frame_untrusted(
        "lookup_balance", "The balance of account 4411 is 1,250.00 USD."
    )
    assert [record.event for record in fakes.audit.records] == [
        "model.call",
        "tool.call",
        "model.call",
    ]
    assert {record.application for record in fakes.audit.records} == {APPLICATION}


async def test_the_agent_remembers_the_conversation_per_caller() -> None:
    fakes = Fakes(model=FakeChatModelProvider(["one", "two", "three"]))
    async with fakes.container() as services:
        await ask(services, context(), "first question")
        await ask(services, context(), "second question")
        await ask(services, context(subject="u-2"), "someone else's question")

    second_prompt, other_prompt = fakes.model.models[1].calls[0], fakes.model.models[2].calls[0]
    assert [m.content for m in second_prompt[1:]] == ["first question", "one", "second question"]
    assert [m.content for m in other_prompt[1:]] == ["someone else's question"]


async def test_the_agent_cannot_run_for_a_caller_past_their_deadline() -> None:
    fakes = Fakes()
    expired = RequestContext(
        principal=Principal(subject="u-1", tenant="t-9"),
        application=APPLICATION,
        request_id="r-1",
        thread_id="th-1",
        deadline=fakes.clock.now(),
    )
    async with fakes.container() as services:
        with pytest.raises(PolicyDenied) as caught:
            await ask(services, expired, "anything")
    assert caught.value.reason_code == "deadline_exceeded"


# ------------------------------------------ the agent, on the real local adapters


async def test_the_agent_runs_on_the_local_adapters_with_no_network(tmp_path: Path) -> None:
    """Everything is real here except the model: JSONL audit, SQLite checkpoints."""
    audit_path = tmp_path / "audit.jsonl"
    checkpoint_path = tmp_path / "checkpoints.sqlite"
    config = ConfigResolver(
        MappingConfigSource(
            {
                "EAP_PROFILE": "local",
                "EAP_MODEL_PROVIDER": "fake",
                "EAP_MODEL_ID": "fake-model",
                "EAP_AUDIT_OPTIONS": json.dumps({"path": str(audit_path)}),
                "EAP_CHECKPOINT_OPTIONS": json.dumps({"path": str(checkpoint_path)}),
                "EAP_IDENTITY_OPTIONS": json.dumps({"subject": "dev", "tenant": "acme"}),
                "EAP_POLICY_OPTIONS": json.dumps({"path": str(RULES)}),
            }
        )
    ).resolve()

    async with ServiceContainer(config) as services:
        await services.validate()
        principal = await services.identity.verify(None)
        request = RequestContext(
            principal=principal, application=APPLICATION, request_id="r-1", thread_id="th-1"
        )
        answer = await ask(services, request, "What is the balance of account 4411?")

    assert answer == "fake: What is the balance of account 4411?"
    (record,) = [json.loads(line) for line in audit_path.read_text("utf-8").splitlines()]
    assert record["event"] == "model.call"
    assert record["outcome"] == "success"
    assert (record["tenant"], record["subject"]) == ("acme", "dev")
    assert "4411" not in json.dumps(record)
    assert checkpoint_path.exists()


def test_the_command_line_prints_the_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("EAP_MODEL_PROVIDER", "fake")
    monkeypatch.setenv("EAP_MODEL_ID", "fake-model")
    monkeypatch.setenv("EAP_POLICY_OPTIONS", json.dumps({"path": str(RULES)}))
    assert main(["Hello there"]) == 0
    assert capsys.readouterr().out == "fake: Hello there\n"
    assert (tmp_path / ".agentlib" / "audit.jsonl").exists()
    assert (tmp_path / ".agentlib" / "checkpoints.sqlite").exists()


def test_the_command_line_reports_configuration_problems(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("EAP_MODEL_PROVIDR", "fake")
    assert main(["Hello"]) == 1
    assert "did you mean EAP_MODEL_PROVIDER" in capsys.readouterr().err
