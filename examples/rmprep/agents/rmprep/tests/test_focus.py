"""Accepted context survives topic changes, refreshes and historical date references."""

from typing import cast

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from rmprep.context import ContextBuilder
from rmprep.focus import conversation_focus
from rmprep.router import router
from rmprep.selection import ContextSelection, select_context
from rmprep.specialist import _accepted_update
from rmprep.state import State

from .test_context_management import JONES, catalog


@pytest.mark.parametrize(
    "topic", ["debt", "cash", "revenue", "their profitability", "Cash Flow", "Total Debt"]
)
def test_topic_followup_keeps_client_and_memo(topic: str) -> None:
    history, memos = catalog()
    state: State = {
        "messages": [*history, HumanMessage(f"What about {topic}?")],
        "memos": memos,
        "active_client": JONES,
        "active_memo_id": memos[1]["memo_id"],
    }
    selected = cast(ContextSelection, router(state)["selection"])
    assert selected["uen"] == JONES
    assert selected["memo_ids"] == [memos[1]["memo_id"]]


def test_qa_date_survives_followup_without_a_memo() -> None:
    state: State = {
        "messages": [HumanMessage("Show JONE-0022 financials as of 2026-08-03", id="q")]
    }
    state = cast(State, {**state, **router(state)})
    accepted = _accepted_update(
        state, AIMessage("Financial summary", id="a"), None, state["selection"], {}
    )
    state = cast(
        State,
        {
            **state,
            **accepted,
            "messages": [
                *state["messages"],
                *accepted["messages"],
                HumanMessage("What about covenants?"),
            ],
        },
    )
    selection = cast(ContextSelection, router(state)["selection"])
    assert selection["uen"] == JONES
    assert selection["meeting_date"] == "2026-08-03"
    assert selection["retrieval_date"] == "2026-08-03"


def test_refresh_followup_keeps_fresh_answer_instead_of_old_memo() -> None:
    history, memos = catalog()
    state: State = {
        "messages": [*history, HumanMessage("What are the current financials?", id="refresh")],
        "memos": memos,
        "active_client": JONES,
        "active_memo_id": memos[1]["memo_id"],
    }
    state = cast(State, {**state, **router(state)})
    selection: ContextSelection = {**state["selection"], "retrieval_date": "2026-10-12"}
    accepted = _accepted_update(
        state, AIMessage("Fresh October figures", id="fresh"), None, selection, {}
    )
    state = cast(
        State,
        {
            **state,
            **accepted,
            "messages": [
                *state["messages"],
                *accepted["messages"],
                HumanMessage("How did you calculate that?", id="followup"),
            ],
        },
    )
    selected = cast(ContextSelection, router(state)["selection"])
    built = ContextBuilder().build(state, "Use evidence", selected)
    assert selected["retrieval_date"] == "2026-10-12"
    assert not selected["memo_ids"]
    assert "fresh" in [message.id for message in built.messages]
    # An explicit historical reference still selects its original snapshot.
    old = select_context(
        f"Explain {memos[0]['memo_id']}",
        memos,
        "",
        JONES,
        creating_memo=False,
        focus=state["focus"],
    )
    assert old["meeting_date"] == "2026-07-03"


def test_refresh_by_memo_id_uses_explicit_new_date() -> None:
    _, memos = catalog()
    selection = select_context(
        f"Refresh {memos[1]['memo_id']} as of 2026-10-12",
        memos,
        memos[1]["memo_id"],
        JONES,
        creating_memo=True,
    )
    assert selection["meeting_date"] == "2026-10-12"
    assert selection["uen"] == JONES
    assert not selection["memo_ids"]  # A new version requires new evidence.


def test_unresolved_lookup_does_not_overwrite_accepted_focus() -> None:
    history, memos = catalog()
    state: State = {
        "messages": [*history, HumanMessage("What about Another Company?")],
        "memos": memos,
        "active_client": JONES,
        "active_memo_id": memos[1]["memo_id"],
    }
    state = cast(State, {**state, **router(state)})
    previous = conversation_focus(state)
    update = _accepted_update(
        state, AIMessage("Please identify the company."), None, state["selection"], {}
    )
    assert conversation_focus(cast(State, {**state, **update})) == previous


def test_historical_selection_does_not_keep_newer_focus_anchors() -> None:
    history, memos = catalog()
    state: State = {
        "messages": [*history, HumanMessage("Explain the July memo")],
        "memos": memos,
        "active_client": JONES,
        "focus": {
            "client_uens": [JONES],
            "memo_ids": [],
            "evidence_date": "2026-10-12",
            "message_ids": ["october-question", "october-call", "october-result", "october-answer"],
        },
    }
    state = cast(State, {**state, **router(state)})
    update = _accepted_update(state, AIMessage("July explanation"), None, state["selection"], {})
    assert update["focus"]["evidence_date"] == "2026-07-03"
    assert not update["focus"]["message_ids"]


def test_refresh_honors_an_explicit_retrieval_date() -> None:
    _, memos = catalog()
    selection = select_context(
        "Retrieve current financials as of 2026-08-03",
        memos,
        memos[0]["memo_id"],
        JONES,
        creating_memo=False,
    )
    assert selection["retrieval_date"] == "2026-08-03"
