"""Behavioral checks for recording and refusing viewer feedback on answers."""

import json
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import httpx
import pytest
from fastapi import HTTPException

from ai_agent_lib_core.evaluation.feedback import JsonlFeedbackLog, load_feedback
from ai_agent_lib_ui.adapters.catalog import FileCatalog, LocalAccess, pin_bindings, read_registries
from ai_agent_lib_ui.adapters.http import HttpDispatcher
from ai_agent_lib_ui.adapters.sqlite import SqliteThreads
from ai_agent_lib_ui.application.chat import ChatService
from ai_agent_lib_ui.config.settings import LocalIdentity, Settings
from ai_agent_lib_ui.contracts.conversations import RunRecord
from ai_agent_lib_ui.integrations.answer_format import answer_id
from ai_agent_lib_ui.integrations.runtime import Runtime
from ai_agent_lib_ui.integrations.security import session_token

# chainlit's import chain (via literalai/traceloop) raises a pydantic deprecation
# warning that this project's `filterwarnings = ["error"]` turns fatal. The chat
# tests avoid it by importing chainlit-dependent names only inside test/fixture
# bodies, under this file-wide filter, rather than at module scope during collection.
if TYPE_CHECKING:
    from chainlit.types import Feedback

    from ai_agent_lib_ui.integrations.data_layer import DataLayer

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

FeedbackFactory = Callable[..., "Feedback"]
Environment = tuple["DataLayer", RunRecord, str, Path, FeedbackFactory]


def settings_at(path: Path) -> Settings:
    agents = path / "agents.json"
    tools = path / "tools.json"
    agents.write_text(
        json.dumps(
            {
                "schema": "agentcraft2.registry/v1",
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
    tools.write_text('{"schema":"agentcraft2.registry/v1","servers":[]}', encoding="utf-8")
    snapshot, _ = read_registries(agents, tools)
    urls = frozenset({"http://127.0.0.1:8199/memo"})
    return Settings(
        agents_path=agents,
        tools_path=tools,
        identity=LocalIdentity(),
        allowed_urls=urls,
        bindings=pin_bindings(snapshot, ("memo",), urls),
    )


async def _respond(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    return httpx.Response(
        200,
        json={
            "thread_id": body["thread_id"],
            "request_id": request.headers["x-request-id"],
            "output": {"answer": "Here is the answer"},
        },
    )


@pytest.fixture
async def environment(tmp_path: Path) -> AsyncIterator[Environment]:
    """One composed runtime with a single completed run, as `data_layer` sees it."""
    from chainlit.types import Feedback

    from ai_agent_lib_ui.integrations.data_layer import DataLayer

    settings = settings_at(tmp_path)
    access = LocalAccess(settings)
    store = SqliteThreads(tmp_path / "ui.sqlite", access, b"test-cursor-secret")
    feedback_path = tmp_path / "feedback.jsonl"
    transport = httpx.MockTransport(_respond)
    async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
        catalog = FileCatalog(settings, access)
        service = ChatService(store, catalog, HttpDispatcher(client, 2))
        principal = settings.identity.principal()
        thread_id = (await service.create(principal, "memo")).thread.thread_id
        run = await service.submit(principal, thread_id, "one", "What is the answer?")
        assert run.state == "completed"
        resources = Runtime(
            settings,
            store,
            catalog,
            service,
            client,
            "test-auth-secret",
            "test-access-code",
            JsonlFeedbackLog(feedback_path),
        )
        token = await store.new_session(resources.identity)
        reset_session = session_token.set(token)

        def feedback_for(
            value: Literal[0, 1], *, for_id: str | None = None, comment: str | None = None
        ) -> Feedback:
            return Feedback(
                forId=for_id if for_id is not None else answer_id(run),
                value=value,
                threadId=thread_id,
                comment=comment,
            )

        try:
            yield DataLayer(resources), run, thread_id, feedback_path, feedback_for
        finally:
            session_token.reset(reset_session)
            await service.close()


async def test_feedback_on_an_accepted_answer_is_recorded(environment: Environment) -> None:
    data_layer, run, thread_id, feedback_path, feedback_for = environment
    await data_layer.upsert_feedback(feedback_for(1, comment="helpful"))
    records = load_feedback(feedback_path)
    assert [record.verdict for record in records] == ["up"]
    assert records[0].request_id == run.request_id
    assert records[0].thread_id == thread_id
    assert records[0].comment == "helpful"
    assert records[0].subject  # the signed-in user, never empty


async def test_value_zero_is_a_down_verdict(environment: Environment) -> None:
    data_layer, _run, _thread_id, feedback_path, feedback_for = environment
    await data_layer.upsert_feedback(feedback_for(0))
    records = load_feedback(feedback_path)
    assert records[0].verdict == "down"


async def test_feedback_for_an_unknown_step_is_refused(environment: Environment) -> None:
    data_layer, _run, _thread_id, feedback_path, feedback_for = environment
    with pytest.raises(HTTPException) as caught:
        await data_layer.upsert_feedback(feedback_for(1, for_id="not-a-real-answer"))
    assert caught.value.status_code == 404
    assert not feedback_path.exists()


async def test_delete_feedback_stays_refused(environment: Environment) -> None:
    data_layer, _run, _thread_id, _feedback_path, _feedback_for = environment
    with pytest.raises(HTTPException):
        await data_layer.delete_feedback("any")
