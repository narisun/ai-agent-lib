"""Behavioral checks for exporting saved conversations as interaction records."""

import json
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from ai_agent_lib_core.contracts import Principal
from ai_agent_lib_core.evaluation.feedback import FeedbackRecord, JsonlFeedbackLog
from ai_agent_lib_core.evaluation.online import load_interactions
from ai_agent_lib_ui.adapters.catalog import FileCatalog, LocalAccess, pin_bindings, read_registries
from ai_agent_lib_ui.adapters.http import HttpDispatcher
from ai_agent_lib_ui.adapters.interactions import export_interactions
from ai_agent_lib_ui.adapters.sqlite import SqliteThreads
from ai_agent_lib_ui.application.chat import ChatService, view_for
from ai_agent_lib_ui.config.settings import LocalIdentity, Settings
from ai_agent_lib_ui.contracts.conversations import RunRecord

Environment = tuple[SqliteThreads, Principal, RunRecord]


def settings_at(path: Path) -> Settings:
    agents = path / "agents.json"
    tools = path / "tools.json"
    agents.write_text(
        json.dumps(
            {
                "schema": "agentlib.registry/v1",
                "agents": [
                    {
                        "id": "memo",
                        "owner": "team",
                        "version": "1",
                        "url": "http://127.0.0.1:8199/memo",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    tools.write_text('{"schema":"agentlib.registry/v1","servers":[]}', encoding="utf-8")
    snapshot, _ = read_registries(agents, tools)
    urls = frozenset({"http://127.0.0.1:8199/memo"})
    return Settings(
        agents_path=agents,
        tools_path=tools,
        identity=LocalIdentity(),
        allowed_urls=urls,
        bindings=pin_bindings(snapshot, ("memo",), urls),
    )


class _ScriptedResponder:
    """Answer the first call; fail every later call, so a thread holds both."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        if self.calls == 1:
            body = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "thread_id": body["thread_id"],
                    "request_id": request.headers["x-request-id"],
                    "output": {"answer": "Here is the answer"},
                },
            )
        return httpx.Response(403, json={"detail": "denied"})


@pytest.fixture
async def environment(tmp_path: Path) -> AsyncIterator[Environment]:
    """One owned thread holding a completed run and a failed run."""
    settings = settings_at(tmp_path)
    access = LocalAccess(settings)
    store = SqliteThreads(tmp_path / "ui.sqlite", access, b"test-cursor-secret")
    transport = httpx.MockTransport(_ScriptedResponder())
    async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
        catalog = FileCatalog(settings, access)
        service = ChatService(store, catalog, HttpDispatcher(client, 2))
        principal = settings.identity.principal()
        thread_id = (await service.create(principal, "memo")).thread.thread_id
        completed = await service.submit(principal, thread_id, "one", "What is the answer?")
        assert completed.state == "completed"
        failed = await service.submit(principal, thread_id, "two", "Will this also work?")
        assert failed.state == "failed"
        try:
            yield store, principal, completed
        finally:
            await service.close()


async def test_export_writes_one_interaction_per_completed_run(
    tmp_path: Path, environment: Environment
) -> None:
    store, principal, _completed = environment
    count = await export_interactions(store, principal, tmp_path / "out.jsonl")
    assert count == 1
    [item] = load_interactions(tmp_path / "out.jsonl")
    assert item.question
    assert item.answer
    assert item.verdict == ""


async def test_export_joins_feedback_by_request_id(
    tmp_path: Path, environment: Environment
) -> None:
    store, principal, completed = environment
    feedback_path = tmp_path / "feedback.jsonl"
    log = JsonlFeedbackLog(feedback_path)
    await log.write(
        FeedbackRecord(
            request_id=completed.request_id,
            thread_id=completed.thread_id,
            application=completed.scope.application,
            subject=principal.subject,
            verdict="down",
        )
    )
    count = await export_interactions(
        store, principal, tmp_path / "out.jsonl", feedback_path=feedback_path
    )
    assert count == 1
    [item] = load_interactions(tmp_path / "out.jsonl")
    assert item.verdict == "down"


async def test_export_feedback_join_lets_the_latest_line_win(
    tmp_path: Path, environment: Environment
) -> None:
    store, principal, completed = environment
    feedback_path = tmp_path / "feedback.jsonl"
    log = JsonlFeedbackLog(feedback_path)
    await log.write(
        FeedbackRecord(
            request_id=completed.request_id,
            thread_id=completed.thread_id,
            application=completed.scope.application,
            subject=principal.subject,
            verdict="down",
        )
    )
    await log.write(
        FeedbackRecord(
            request_id=completed.request_id,
            thread_id=completed.thread_id,
            application=completed.scope.application,
            subject=principal.subject,
            verdict="up",
        )
    )
    count = await export_interactions(
        store, principal, tmp_path / "out.jsonl", feedback_path=feedback_path
    )
    assert count == 1
    [item] = load_interactions(tmp_path / "out.jsonl")
    assert item.verdict == "up"


async def test_export_without_a_feedback_file_is_not_an_error(
    tmp_path: Path, environment: Environment
) -> None:
    store, principal, _completed = environment
    count = await export_interactions(
        store, principal, tmp_path / "out.jsonl", feedback_path=tmp_path / "missing.jsonl"
    )
    assert count == 1


async def test_export_includes_archived_threads(tmp_path: Path, environment: Environment) -> None:
    store, principal, completed = environment
    await store.change(
        view_for(principal, completed.scope.application), completed.thread_id, archived=True
    )
    count = await export_interactions(store, principal, tmp_path / "out.jsonl")
    assert count == 1
    [item] = load_interactions(tmp_path / "out.jsonl")
    assert item.thread_id == completed.thread_id
