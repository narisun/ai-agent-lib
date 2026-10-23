"""Files written before the rename carry ``agentlib.*`` schema identifiers and still load."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from agentcraft2_core.adapters.policy_documents import parse_rules_document
from agentcraft2_core.adapters.registry_documents import (
    parse_agents_document,
    parse_tools_document,
)
from agentcraft2_core.adapters.strict_yaml import load_strict_yaml
from agentcraft2_core.contracts import RequestOutcome, Scope, current_schema, is_schema
from agentcraft2_core.contracts.errors import ValidationFailed
from agentcraft2_core.evaluation.baseline import load_report
from agentcraft2_core.evaluation.feedback import load_feedback
from agentcraft2_core.evaluation.online import load_interactions
from agentcraft2_core.integrations.langgraph.history import (
    LEGACY_REQUEST_KEY,
    REQUEST_KEY,
    request_history,
)
from agentcraft2_core.testing import FrozenClock

OLD = "agentlib."
NEW = "agentcraft2."

TOOLS = """\
schema: agentcraft2.registry/v1
servers:
  - id: accounts
    owner: treasury-data
    url: http://localhost:8001/mcp
    audience: accounts-mcp
    tools:
      - name: accounts.lookup
        version: 1.2.0
        schema_sha256: {pin}
        classification: restricted
        read_only: true
""".format(pin="0f" * 32)


def _old(text: str) -> str:
    return text.replace(NEW, OLD)


def test_the_old_prefix_reads_as_the_new_one_and_nothing_else_changes() -> None:
    assert current_schema("agentlib.registry/v1") == "agentcraft2.registry/v1"
    assert current_schema("agentcraft2.registry/v1") == "agentcraft2.registry/v1"
    assert current_schema("other.registry/v1") == "other.registry/v1"
    assert current_schema(7) == 7
    assert is_schema("agentlib.rules/v1", "agentcraft2.rules/v1")
    assert not is_schema("agentlib.rules/v2", "agentcraft2.rules/v1")
    assert not is_schema(None, "agentcraft2.rules/v1")


def test_a_tool_registry_with_the_old_schema_loads_the_same() -> None:
    assert "agentlib.registry/v1" in _old(TOOLS)
    assert parse_tools_document(load_strict_yaml(_old(TOOLS))) == parse_tools_document(
        load_strict_yaml(TOOLS)
    )


def test_an_agent_registry_with_the_old_schema_loads() -> None:
    assert parse_agents_document({"schema": "agentlib.registry/v1", "agents": []}) == ()


def test_a_rules_document_with_the_old_schema_loads_the_same() -> None:
    rules = [{"id": "r1", "actions": ["data.query"], "roles": ["analyst"]}]
    assert parse_rules_document({"schema": "agentlib.rules/v1", "rules": rules}) == (
        parse_rules_document({"schema": "agentcraft2.rules/v1", "rules": rules})
    )


def test_a_later_version_of_the_old_prefix_is_still_refused() -> None:
    with pytest.raises(Exception, match="rules"):
        parse_rules_document({"schema": "agentlib.rules/v2", "rules": []})


def test_a_saved_eval_report_with_the_old_schema_loads(tmp_path: Path) -> None:
    path = tmp_path / "baseline.json"
    body = {"schema": "agentlib.eval-report/v1", "means": {"m": 1.0}, "results": []}
    path.write_text(json.dumps(body), encoding="utf-8")
    assert load_report(path).means == {"m": 1.0}
    path.write_text(json.dumps({**body, "schema": "agentlib.eval-report/v2"}), encoding="utf-8")
    with pytest.raises(ValidationFailed):
        load_report(path)


def test_feedback_and_interactions_with_the_old_schema_load(tmp_path: Path) -> None:
    feedback = tmp_path / "feedback.jsonl"
    feedback.write_text(
        json.dumps(
            {
                "schema": "agentlib.feedback/v1",
                "request_id": "r",
                "thread_id": "t",
                "application": "a",
                "subject": "s",
                "verdict": "up",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    assert [item.verdict for item in load_feedback(feedback)] == ["up"]
    interactions = tmp_path / "interactions.jsonl"
    interactions.write_text(
        json.dumps(
            {
                "schema": "agentlib.interaction/v1",
                "request_id": "r",
                "thread_id": "t",
                "question": "q",
                "answer": "a",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    assert [item.question for item in load_interactions(interactions)] == ["q"]


def test_a_stored_outcome_with_the_old_schema_loads() -> None:
    outcome = RequestOutcome(
        scope=Scope(tenant="t", subject="s", application="a"),
        thread_id="thread",
        request_id="request",
        state="unknown",
        updated_at=FrozenClock().now(),
    )
    stored = outcome.model_dump(by_alias=True, mode="json")
    assert stored["schema"] == "agentcraft2.request-outcome/v1"
    stored["schema"] = "agentlib.request-outcome/v1"
    assert RequestOutcome.model_validate(stored) == outcome
    stored["schema"] = "agentlib.request-outcome/v2"
    with pytest.raises(ValidationError):
        RequestOutcome.model_validate(stored)


class _Graph:
    """Answers history queries from a table keyed by metadata key."""

    def __init__(self, recorded: dict[str, list[str]]) -> None:
        self.recorded = recorded
        self.asked: list[str] = []

    async def aget_state_history(
        self,
        config: dict[str, Any],
        *,
        filter: dict[str, str],  # noqa: A002 - the graph API's own keyword
        limit: int,
    ) -> AsyncIterator[str]:
        (key,) = filter
        self.asked.append(key)
        for snapshot in self.recorded.get(key, [])[:limit]:
            yield snapshot


async def _history(graph: _Graph) -> list[str]:
    return [item async for item in request_history(graph, {}, "request-1", limit=5)]


async def test_request_history_reads_checkpoints_written_under_the_old_key() -> None:
    graph = _Graph({LEGACY_REQUEST_KEY: ["old-1", "old-2"]})
    assert await _history(graph) == ["old-1", "old-2"]
    assert graph.asked == [REQUEST_KEY, LEGACY_REQUEST_KEY]


async def test_request_history_prefers_the_current_key_and_stops_there() -> None:
    graph = _Graph({REQUEST_KEY: ["new-1"], LEGACY_REQUEST_KEY: ["old-1"]})
    assert await _history(graph) == ["new-1"]
    assert graph.asked == [REQUEST_KEY]


async def test_request_history_is_empty_when_neither_key_recorded_anything() -> None:
    assert await _history(_Graph({})) == []
