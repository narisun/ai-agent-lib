"""Versioned resources and stable instructions across request context and tool calls."""

from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from rmprep.context import ContextBuilder
from rmprep.prompts import COMMON, MEMO_PROMPT, QNA_PROMPT, load_prompt
from rmprep.selection import select_context
from rmprep.state import State


def test_prompt_versions_are_explicit_package_resources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert load_prompt("rmprep", "common", 6) == COMMON
    assert "Markdown heading/list markers and divider-only lines do not count" in COMMON
    assert COMMON + load_prompt("memoagent", "system", 2) == MEMO_PROMPT
    assert COMMON + load_prompt("qnaagent", "system", 3) == QNA_PROMPT
    assert "You are memoagent" in MEMO_PROMPT
    assert "You are qnaagent" in QNA_PROMPT
    with pytest.raises(FileNotFoundError):
        load_prompt("memoagent", "system", 99)


@pytest.mark.parametrize(
    "agent,kind,version", [("../x", "system", 1), ("x", "../system", 1), ("x", "system", 0)]
)
def test_prompt_resource_names_cannot_escape_the_package(
    agent: str, kind: str, version: int
) -> None:
    with pytest.raises(ValueError):
        load_prompt(agent, kind, version)


def test_static_prefix_stays_identical_as_metadata_and_output_budget_change() -> None:
    builder = ContextBuilder()
    original: State = {"messages": [HumanMessage("Explain wallet share", id="first")]}
    selection = select_context("Explain wallet share", [], "", "JONE-0022", creating_memo=False)
    initial = builder.build(original, QNA_PROMPT, selection, output_budget="At most 350 words.")
    other: State = {"messages": [HumanMessage("Explain revenue", id="other")]}
    switched = builder.build(
        other, QNA_PROMPT, {**selection, "uen": "SHIN-0039"}, output_budget="At most 200 words."
    )
    assert initial.messages[0].content[0] == switched.messages[0].content[0]
    assert initial.messages[0].content[0] == {"type": "text", "text": QNA_PROMPT}
    assert initial.messages[0].content[1] != switched.messages[0].content[1]
    assert original["messages"][0].id == "first"
    assert initial.diagnostics["included_message_ids"] == ["first"]
    assert "included_message_ids" not in str(initial.messages[0].content)
    assert "estimated_input_tokens" not in str(initial.messages[0].content)


def test_tool_loop_retains_the_entire_preceding_prefix_when_selection_is_unchanged() -> None:
    builder = ContextBuilder()
    question = HumanMessage("Explain wallet share for JONE-0022", id="first")
    selection = select_context(question.text, [], "", "JONE-0022", creating_memo=False)
    initial = builder.build({"messages": [question]}, QNA_PROMPT, selection)
    state: State = {
        "messages": [
            question,
            AIMessage("", tool_calls=[{"name": "lookup", "args": {}, "id": "lookup-1"}]),
            ToolMessage("Synthetic evidence", tool_call_id="lookup-1", id="tool-result"),
        ]
    }
    after_tool = builder.build(state, QNA_PROMPT, selection)
    assert after_tool.messages[:2] == initial.messages
    included = after_tool.diagnostics["included_message_ids"]
    assert isinstance(included, list)
    assert included[-1] == "tool-result"
