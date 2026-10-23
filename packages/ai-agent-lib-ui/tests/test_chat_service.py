"""ChatService admission, dispatch, resume, restart, registry changes, deadlines, reconciliation."""

import asyncio
import json
import sqlite3
import threading
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from chat_support import ScriptedAgent, chat_at

from ai_agent_lib_core.contracts import Principal
from ai_agent_lib_ui.adapters.catalog import FileCatalog, LocalAccess, pin_bindings, read_registries
from ai_agent_lib_ui.adapters.http import HttpDispatcher
from ai_agent_lib_ui.adapters.sqlite import SqliteThreads
from ai_agent_lib_ui.application.chat import ChatService, view_for
from ai_agent_lib_ui.config.settings import Settings
from ai_agent_lib_ui.contracts.conversations import RunRecord, TurnAdmission
from ai_agent_lib_ui.contracts.queries import Availability, ContractError
from ai_agent_lib_ui.integrations.answer_format import outcome_text


async def test_two_questions_restart_resume_and_only_new_input(
    chat: tuple[ChatService, ScriptedAgent, SqliteThreads], settings: Settings
) -> None:
    service, agent, store = chat
    principal = settings.identity.principal()
    thread = (await service.create(principal, "memo")).thread
    first = await service.submit(principal, thread.thread_id, "one", "Prepare a memo")
    second = await service.submit(principal, thread.thread_id, "two", "Explain the cash flow")
    assert first.state == second.state == "completed"
    assert agent.calls == [
        {"input": {"question": "Prepare a memo"}, "thread_id": thread.thread_id},
        {"input": {"question": "Explain the cash flow"}, "thread_id": thread.thread_id},
    ]
    restarted = SqliteThreads(store.path, LocalAccess(settings), b"test-cursor-secret")
    restarted.recover()
    assert (await restarted.get(principal, thread.thread_id)).binding == settings.bindings[0]
    history = await restarted.history(view_for(principal, "memo"), thread.thread_id)
    assert [turn.run.answer for turn in history] == ["answer 1", "answer 2"]
    await service.reattach(principal, thread.thread_id)
    assert len(agent.calls) == 2


async def test_enabling_optional_artifacts_keeps_existing_chats_usable(
    chat: tuple[ChatService, ScriptedAgent, SqliteThreads], settings: Settings
) -> None:
    service, agent, _ = chat
    principal = settings.identity.principal()
    thread = (await service.create(principal, "memo")).thread.thread_id
    bindings = tuple(
        binding.model_copy(
            update={
                "manifest": binding.manifest.model_copy(update={"capabilities": ("artifacts",)})
            }
        )
        for binding in settings.bindings
    )
    updated = settings.model_copy(update={"bindings": bindings})
    service.catalog = FileCatalog(updated, LocalAccess(updated))
    result = await service.submit(principal, thread, "after-upgrade", "Continue this conversation")
    assert result.state == "completed"
    assert len(agent.calls) == 1


async def test_invalid_document_warning_survives_dispatch_and_restart(
    chat: tuple[ChatService, ScriptedAgent, SqliteThreads], settings: Settings
) -> None:
    service, agent, store = chat
    bindings = tuple(
        binding.model_copy(
            update={
                "manifest": binding.manifest.model_copy(update={"capabilities": ("artifacts",)})
            }
        )
        for binding in settings.bindings
    )
    settings = settings.model_copy(update={"bindings": bindings})
    service.catalog = FileCatalog(settings, LocalAccess(settings))
    principal = settings.identity.principal()
    thread = (await service.create(principal, "memo")).thread.thread_id
    agent.mode = "bad_artifact"
    saved = await service.submit(principal, thread, "one", "Generate a memo")
    restarted = SqliteThreads(store.path, LocalAccess(settings), b"test-cursor-secret")
    restored = (await restarted.history(view_for(principal, "memo"), thread))[0].run
    assert restored == saved
    assert restored.answer == "Saved answer"
    assert restored.state == "completed"
    assert restored.artifact_status == Availability.UNAVAILABLE
    assert restored.artifact_warning == "invalid_artifacts"
    assert "invalid document" in outcome_text(restored)
    # Pre-upgrade records still load without migration or transcript changes.
    legacy = RunRecord.model_validate(
        saved.model_dump(exclude={"artifact_status", "artifact_warning"})
    )
    assert legacy.answer == saved.answer
    assert legacy.artifact_warning is None


async def test_stored_run_with_retired_trace_provider_still_loads(
    chat: tuple[ChatService, ScriptedAgent, SqliteThreads], settings: Settings
) -> None:
    """A row saved while Langfuse links existed reloads with its answer and no link."""
    service, _, store = chat
    principal = settings.identity.principal()
    thread = (await service.create(principal, "memo")).thread.thread_id
    saved = await service.submit(principal, thread, "one", "A question")
    trace_id = "1234567890abcdef1234567890abcdef"
    with store.connection() as db:
        row = db.execute("SELECT record FROM runs WHERE id=?", (saved.run_id,)).fetchone()
        record = json.loads(row[0])
        record["trace"] = {
            "provider": "langfuse",
            "trace_id": trace_id,
            "url": f"https://traces.example/project/trial/traces/{trace_id}",
        }
        db.execute("UPDATE runs SET record=? WHERE id=?", (json.dumps(record), saved.run_id))
    restored = (await store.history(view_for(principal, "memo"), thread))[0].run
    assert restored.trace is None
    assert restored == saved


async def test_duplicate_disconnect_and_same_thread_race(
    chat: tuple[ChatService, ScriptedAgent, SqliteThreads], settings: Settings
) -> None:
    service, agent, store = chat
    principal = settings.identity.principal()
    thread = (await service.create(principal, "memo")).thread.thread_id
    agent.release.clear()
    browser = asyncio.create_task(service.submit(principal, thread, "same", "hello"))
    await agent.entered.wait()
    duplicate = asyncio.create_task(service.submit(principal, thread, "same", "hello"))
    browser.cancel()
    with pytest.raises(asyncio.CancelledError):
        await browser
    with pytest.raises(ContractError, match="conflict"):
        await service.submit(principal, thread, "other", "another question")
    with pytest.raises(ContractError, match="conflict"):
        await service.submit(principal, thread, "same", "changed question")
    agent.release.set()
    saved = await duplicate
    replay = await service.submit(principal, thread, "same", "hello")
    assert saved == replay
    assert saved.state == "completed"
    assert len(agent.calls) == 1
    assert len(await store.history(view_for(principal, "memo"), thread)) == 1


async def test_disconnect_during_commit_still_dispatches_and_shutdown_drains(
    settings: Settings, tmp_path: Path
) -> None:
    entered, release = threading.Event(), threading.Event()

    def hold_commit() -> None:
        entered.set()
        assert release.wait(5)

    async with chat_at(settings, tmp_path, commit_hook=hold_commit) as (service, agent, store):
        principal = settings.identity.principal()
        thread = (await service.create(principal, "memo")).thread.thread_id
        browser = asyncio.create_task(service.submit(principal, thread, "one", "hello"))
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            browser.cancel()
            with pytest.raises(asyncio.CancelledError):
                await browser
            closing = asyncio.create_task(service.close())
            await asyncio.sleep(0)
            assert not closing.done()
        finally:
            release.set()
        await asyncio.wait_for(closing, 5)
        history = await store.history(view_for(principal, "memo"), thread)
        assert history[0].run.state == "completed"
        assert len(agent.calls) == 1
        assert not service.admissions
        assert not service.tasks


async def test_restart_releases_accepted_run_without_replaying_it(
    chat: tuple[ChatService, ScriptedAgent, SqliteThreads], settings: Settings
) -> None:
    service, agent, store = chat
    principal = settings.identity.principal()
    thread = (await service.create(principal, "memo")).thread.thread_id
    view = view_for(principal, "memo")
    await store.admit(
        view,
        TurnAdmission(scope=view.scope, thread_id=thread, idempotency_key="old", question="hi"),
    )
    store.recover()
    assert (await service.submit(principal, thread, "old", "hi")).state == "failed"
    assert not agent.calls
    assert (await service.submit(principal, thread, "new", "hello")).state == "completed"
    assert len(agent.calls) == 1


@pytest.mark.parametrize("mode", ["timeout", "invalid", "large", "redirect", "gateway"])
async def test_uncertain_outcome_holds_admission_without_retry(
    chat: tuple[ChatService, ScriptedAgent, SqliteThreads], settings: Settings, mode: str
) -> None:
    service, agent, store = chat
    agent.mode = mode
    principal = settings.identity.principal()
    thread = (await service.create(principal, "memo")).thread.thread_id
    result = await service.submit(principal, thread, "one", "hello")
    assert result.state == "unknown"
    assert result.answer is None
    store.recover()
    assert (await service.submit(principal, thread, "one", "hello")).state == "unknown"
    with pytest.raises(ContractError, match="conflict"):
        await service.submit(principal, thread, "two", "try again")
    assert len(agent.calls) == 1


@pytest.mark.parametrize("mode", ["failed", "thread_busy"])
async def test_definitive_http_failure_allows_new_turn_but_never_replays_old_key(
    chat: tuple[ChatService, ScriptedAgent, SqliteThreads],
    settings: Settings,
    mode: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    service, agent, _ = chat
    principal = settings.identity.principal()
    thread = (await service.create(principal, "memo")).thread.thread_id
    agent.mode = mode
    with caplog.at_level("WARNING"):
        failed = await service.submit(principal, thread, "one", "hello")
    assert failed.state == "failed"
    if mode == "failed":
        # A refusal without an envelope reaches the log with its status, never its body.
        degraded = [r for r in caplog.records if r.getMessage().startswith("agent dispatch")]
        assert len(degraded) == 1
        assert getattr(degraded[0], "status", None) == 403
        assert getattr(degraded[0], "request_id", None) == failed.request_id
        assert "denied" not in caplog.text
    agent.mode = "ok"
    assert await service.submit(principal, thread, "one", "hello") == failed
    assert (await service.submit(principal, thread, "two", "new question")).state == "completed"
    assert len(agent.calls) == 2


async def test_persistence_failure_before_dispatch_makes_no_call(
    chat: tuple[ChatService, ScriptedAgent, SqliteThreads], settings: Settings
) -> None:
    service, agent, store = chat
    principal = settings.identity.principal()
    thread = (await service.create(principal, "memo")).thread.thread_id
    with (
        patch.object(store, "admit", side_effect=sqlite3.OperationalError("disk full")),
        pytest.raises(sqlite3.OperationalError),
    ):
        await service.submit(principal, thread, "one", "hello")
    assert agent.calls == []


async def test_failed_result_commit_preserves_admission_and_restart_marks_unknown(
    chat: tuple[ChatService, ScriptedAgent, SqliteThreads], settings: Settings
) -> None:
    service, agent, store = chat
    principal = settings.identity.principal()
    thread = (await service.create(principal, "memo")).thread.thread_id
    agent.release.clear()
    browser = asyncio.create_task(service.submit(principal, thread, "one", "hello"))
    await agent.entered.wait()
    with patch.object(store, "update", side_effect=sqlite3.OperationalError("disk full")):
        agent.release.set()
        with pytest.raises(sqlite3.OperationalError):
            await browser
    store.recover()
    history = await store.history(view_for(principal, "memo"), thread)
    assert history[0].run.state == "unknown"
    assert history[0].run.answer is None
    with pytest.raises(ContractError, match="conflict"):
        await service.submit(principal, thread, "two", "try again")
    assert len(agent.calls) == 1


async def test_registry_change_after_selection_fails_before_http(
    chat: tuple[ChatService, ScriptedAgent, SqliteThreads],
    settings: Settings,
    caplog: pytest.LogCaptureFixture,
) -> None:
    service, agent, _ = chat
    principal = settings.identity.principal()
    thread = (await service.create(principal, "memo")).thread.thread_id
    settings.agents_path.write_text(settings.agents_path.read_text().replace('"1"', '"2"'))
    with caplog.at_level("WARNING"):
        assert (await service.submit(principal, thread, "one", "hello")).state == "failed"
    assert agent.calls == []
    before = [r for r in caplog.records if r.getMessage().startswith("catalog resolution failed")]
    assert len(before) == 1
    assert getattr(before[0], "error_type", None) == "ContractError"
    with pytest.raises(ContractError, match="stale_binding"):
        await service.create(principal, "memo")


async def test_denied_agent_identity_and_missing_endpoint(settings: Settings) -> None:
    catalog = FileCatalog(settings, LocalAccess(settings))
    for principal, agent in [
        (settings.identity.principal(), "not-allowed"),
        (Principal(subject="other", tenant="dev-tenant"), "memo"),
    ]:
        with pytest.raises(ContractError, match="not_found"):
            await catalog.resolve(principal, agent)
    raw = json.loads(settings.agents_path.read_text())
    raw["agents"][0].pop("url")
    settings.agents_path.write_text(json.dumps(raw))
    agents, _ = read_registries(settings.agents_path, settings.tools_path)
    with pytest.raises(ValueError, match="loopback url"):
        pin_bindings(agents, ("memo",), settings.allowed_urls)


async def test_two_connections_cannot_admit_competing_turns(
    settings: Settings, tmp_path: Path
) -> None:
    principal = settings.identity.principal()
    view = view_for(principal, "memo")
    stores = [
        SqliteThreads(tmp_path / "race.sqlite", LocalAccess(settings), b"key") for _ in range(2)
    ]
    await stores[0].create(view, settings.bindings[0], "thread", "title")
    turns = [
        TurnAdmission(
            scope=view.scope, thread_id="thread", idempotency_key=f"key{i}", question="hi"
        )
        for i in range(2)
    ]
    # Run independent event loops in worker threads to exercise the database index,
    # rather than merely serializing two methods on this test's event loop.
    results = await asyncio.gather(
        *[
            asyncio.to_thread(asyncio.run, store.admit(view, turn))
            for store, turn in zip(stores, turns, strict=True)
        ],
        return_exceptions=True,
    )
    assert sum(isinstance(result, ContractError) for result in results) == 1
    assert len(await stores[0].history(view, "thread")) == 1


async def test_total_deadline_also_bounds_a_stalled_dispatch(
    chat: tuple[ChatService, ScriptedAgent, SqliteThreads], settings: Settings
) -> None:
    service, agent, _ = chat
    agent.release.clear()
    principal = settings.identity.principal()
    thread = (await service.create(principal, "memo")).thread.thread_id
    async with asyncio.timeout(4):
        result = await service.submit(principal, thread, "one", "hello")
    assert result.state == "unknown"
    assert len(agent.calls) == 1


async def test_json_wrong_request_id_holds_admission(settings: Settings, tmp_path: Path) -> None:
    access = LocalAccess(settings)
    store = SqliteThreads(tmp_path / "correlation.sqlite", access, b"key")

    async def wrong(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "thread_id": json.loads(request.content)["thread_id"],
                "request_id": "another-request",
                "output": {"answer": "wrong invocation"},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(wrong)) as client:
        chat = ChatService(store, FileCatalog(settings, access), HttpDispatcher(client, 2))
        principal = settings.identity.principal()
        thread = (await chat.create(principal, "memo")).thread.thread_id
        result = await chat.submit(principal, thread, "one", "Question")
        assert result.state == "unknown"
        assert result.answer is None
        assert result.request_id != "another-request"
        with pytest.raises(ContractError, match="conflict"):
            await chat.submit(principal, thread, "two", "Do not resend")


async def test_unrelated_registry_change_preserves_existing_thread(
    chat: tuple[ChatService, ScriptedAgent, SqliteThreads],
    settings: Settings,
) -> None:
    service, agent, _ = chat
    principal = settings.identity.principal()
    thread = (await service.create(principal, "memo")).thread.thread_id
    old_revision = settings.bindings[0].manifest.registry_revision
    raw = json.loads(settings.agents_path.read_text())
    raw["agents"][1]["version"] = "2"
    settings.agents_path.write_text(json.dumps(raw))
    result = await service.submit(principal, thread, "one", "Continue")
    assert result.state == "completed"
    assert len(agent.calls) == 1
    assert result.registry_revision != old_revision


@pytest.mark.parametrize("outcome", ["completed", "failed", "pending", "wrong_id", "unavailable"])
async def test_reopen_reconciles_only_confirmed_correlated_outcomes_without_resending(
    settings: Settings,
    tmp_path: Path,
    outcome: str,
) -> None:
    access = LocalAccess(settings)
    store = SqliteThreads(tmp_path / "recovery.sqlite", access, b"key")
    saved: dict[str, object] = {}
    methods: list[str] = []

    async def agent(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        if request.method == "POST":
            saved.update(
                thread_id=json.loads(request.content)["thread_id"],
                request_id=request.headers["x-request-id"],
                elapsed_ms=1250,
                usage={"input_tokens": 20, "output_tokens": 5},
                model_calls=1,
            )
            if outcome == "failed":
                saved["error"] = "invalid"
            else:
                saved["output"] = {"answer": "The already accepted answer"}
            raise httpx.ReadTimeout("response was lost")
        if outcome == "pending":
            return httpx.Response(200, json={"state": "unknown"})
        if outcome == "unavailable":
            return httpx.Response(404)
        return httpx.Response(
            200, json={**saved, **({"request_id": "wrong"} if outcome == "wrong_id" else {})}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(agent)) as client:
        service = ChatService(store, FileCatalog(settings, access), HttpDispatcher(client, 2))
        principal = settings.identity.principal()
        view = view_for(principal, "memo")
        thread = (await service.create(principal, "memo")).thread.thread_id
        unknown = await service.submit(principal, thread, "one", "Question")
        assert unknown.state == "unknown"
        await service.reattach(principal, thread)
        latest = (await store.history(view, thread))[-1].run
        assert methods == ["POST", "GET"]
        if outcome in {"completed", "failed"}:
            assert latest.state == outcome
            assert latest.usage.input_tokens == 20
            assert latest.elapsed_ms == 1250
            with store.connection() as db:
                assert db.execute("SELECT state FROM reconciliations").fetchall() == [(outcome,)]
            await service.reattach(principal, thread)
            assert methods == ["POST", "GET"]
            assert await service.submit(principal, thread, "one", "Question") == latest
        else:
            assert latest == unknown
            with pytest.raises(ContractError, match="conflict"):
                await service.submit(principal, thread, "two", "Not safe to retry")
