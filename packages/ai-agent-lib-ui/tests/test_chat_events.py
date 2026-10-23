"""Server-sent event parsing and live-draft delivery through the dispatcher."""

import asyncio
from pathlib import Path

import httpx
import pytest
from chat_support import EventStream

from ai_agent_lib_ui.adapters.catalog import FileCatalog, LocalAccess
from ai_agent_lib_ui.adapters.http import HttpDispatcher
from ai_agent_lib_ui.adapters.sqlite import SqliteThreads
from ai_agent_lib_ui.application.chat import ChatService, view_for
from ai_agent_lib_ui.application.progress import Draft
from ai_agent_lib_ui.config.settings import Settings
from ai_agent_lib_ui.contracts.queries import ContractError


@pytest.mark.parametrize(
    "body", [b"data: " + b"x" * 65 + b"\n\n", b"data: " + b"x" * 65, b": heartbeat\n\n" * 50]
)
async def test_stream_parser_bounds_frames_and_total_bytes(body: bytes) -> None:
    from ai_agent_lib_ui.adapters.events import events

    with pytest.raises(ContractError, match="invalid_response"):
        async for _ in events(httpx.Response(200, content=body), 64):
            pass


@pytest.mark.parametrize(
    "mode",
    [
        "ok",
        "missing",
        "cross_thread",
        "error",
        "duplicate",
        "internal_error",
        "cross_thread_error",
        "cross_request_error",
        "output_error",
        "duplicate_error",
        "interrupted_error",
    ],
)
async def test_live_drafts_are_not_saved_and_only_one_terminal_result_is_accepted(
    settings: Settings,
    tmp_path: Path,
    mode: str,
) -> None:
    release = asyncio.Event()
    observed = asyncio.Event()
    calls = 0

    async def agent(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        if request.method == "GET":
            return httpx.Response(404)  # Older servers do not have a status journal.
        calls += 1
        assert request.headers["accept"] == "text/event-stream, application/json"
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=EventStream(request, mode, release),
        )

    async def display(draft: Draft) -> None:
        if "Provisional" in draft.text:
            observed.set()

    access = LocalAccess(settings)
    store = SqliteThreads(tmp_path / "stream.sqlite", access, b"key")
    principal = settings.identity.principal()
    async with httpx.AsyncClient(transport=httpx.MockTransport(agent)) as client:
        service = ChatService(store, FileCatalog(settings, access), HttpDispatcher(client, 3))
        thread = (await service.create(principal, "memo")).thread.thread_id
        browser = asyncio.create_task(
            service.submit(principal, thread, "send", "question", on_progress=display)
        )
        await asyncio.wait_for(observed.wait(), timeout=2)
        assert not browser.done()
        history = await store.history(view_for(principal, "memo"), thread)
        assert history[0].run.answer is None
        # Disconnect the browser during delivery. The service must still save the result.
        browser.cancel()
        with pytest.raises(asyncio.CancelledError):
            await browser
        release.set()
        await service.reattach(principal, thread)
        result = await service.submit(principal, thread, "send", "question")
        assert result.state == {"ok": "completed", "error": "failed"}.get(mode, "unknown")
        assert result.elapsed_ms is not None
        if mode == "ok":
            assert result.answer == "Accepted answer"
            assert result.usage.input_tokens == 40
            assert result.usage.output_tokens == 12
            assert result.model_calls == 2
            reopened = SqliteThreads(store.path, access, b"key")
            assert (await reopened.history(view_for(principal, "memo"), thread))[0].run == result
        else:
            assert result.answer is None
        assert calls == 1
        if mode == "error":
            assert result.failure_code == "invalid"
            assert result.usage.input_tokens == 90
            assert result.usage.output_tokens == 20
            assert result.model_calls == 3
            reopened = SqliteThreads(store.path, access, b"key")
            assert (await reopened.history(view_for(principal, "memo"), thread))[0].run == result
            # The original key never dispatches again; a different question is allowed.
            assert await service.submit(principal, thread, "send", "question") == result
            assert calls == 1
            assert (
                await service.submit(principal, thread, "next", "new question")
            ).state == "failed"
            assert calls == 2
        elif mode != "ok":
            with pytest.raises(ContractError):
                await service.submit(principal, thread, "next", "new question")
            assert calls == 1
        await service.close()
